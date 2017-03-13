#! /usr/bin/env python
#
# Command Line Interface to volumed
#
# TODO: maybe add more queries to get mute state.
#

import sys
import socket
import threading
import optparse 
import time

class Receiver(threading.Thread):
    """Thread for processing the results sent back from the volumed
    server."""
    def __init__(self, socket):
        super(Receiver, self).__init__()
        self.socket = socket
        self.buffered = ''
        self.start()

    def get_from_buffer(self):
        if self.buffered != '':
            parts = self.buffered.split("\0", 1)
            if len(parts) == 2:
                self.buffered = parts[1]
                # Deal with non-unix line endings (html?)
                return parts[0].replace(chr(13), chr(10))

    def get_msg(self):
        result = self.get_from_buffer()
        if result:
            return result
        while True:
            chunk = self.socket.recv(1024)
            if chunk == '':
                #print "CLIENTSOCKET END OF INPUT: %s" % clientsocket
                if self.buffered != '':
                    self.buffered += "\0"
                    return self.get_from_buffer()
                return
            self.buffered += chunk
            result = self.get_from_buffer()
            if result:
                return result
        
    def run(self):
        buffer = ""
        while True:
            chunk = self.get_msg()
            print "CHUNK: %s" % chunk
            if chunk:
                buffer = buffer + chunk
                lines = buffer.split('\n')
                if len(lines) > 0:
                    for line in lines[:-1]:
                        print line
                buffer = lines[-1]
            else:
                break
        self.socket.shutdown(socket.SHUT_RDWR)
        self.socket.close()

def sendmsg(socket, msg):
    """Assume that all messages are less than 255 bytes long."""
    totalsent = 0
    while totalsent < len(msg):
        try:
            sent = socket.send(msg[totalsent:])
        except:
            sent = 0
        if sent == 0:
            raise RuntimeError("socket connection broken")
        totalsent = totalsent + sent

def send(socket, msg):
    sendmsg(socket, msg)
    sendmsg(socket, "\0")


if __name__ == '__main__':
    parser = optparse.OptionParser()
    parser.add_option("-H", "--host", dest="hostname", default='localhost',
                      help="Connect to volumed server using specified port")
    parser.add_option("-p", "--port", type=int, dest="port", default=8888,
                      help="Connect to volumed server using specified port")
    parser.add_option("-c", "--command",  dest="command",
                      help="Execute the specified volumed command")

    (options, args) = parser.parse_args()
    if len(args) != 0:
        sys.stderr.write("Unexpected args: %s\n" % " ".join(args))
        sys.exit(2)

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.connect((options.hostname, options.port))
    except socket.error as e:
        sys.stderr.write("Unable to connect to \'%s\':%d.\n  (%s)\n" %
                         (options.hostname, options.port, e))
        sys.exit(2)

        
    r = Receiver(s)
    send(s, "%s\n" % options.command)
    try:
        send(s, "q\n")
    except: pass  # Keep this quiet in case we had sent a shutdown.
    r.join()
    s.close()
