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
        self.start()

    def get_msg(self):
        chunk = self.socket.recv(1)
        if chunk == '':
            return
        len = ord(chunk)
        chunk = self.socket.recv(len)
        if chunk == '':
            return
        return chunk
        
    def run(self):
        buffer = ""
        while True:
            chunk = self.get_msg()
            if chunk:
                buffer = buffer + chunk
                lines = buffer.split('\n')
                if len(lines) > 0:
                    for line in lines[:-1]:
                        print line
                buffer = lines[-1]
            else:
                return

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
    msglen = len(msg)
    sendmsg(socket, chr(msglen))
    sendmsg(socket, msg)


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
    send(s, "q\n")
    r.join()
    s.close()
