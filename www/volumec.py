#! /usr/bin/env python

from ws4py.client.threadedclient import WebSocketClient

class VolumeClient(WebSocketClient):
    def closed(self, code, reason=None):
        if code != 1000:
            print "Closed down", code, reason

    def sendcmd(self, cmd):
        self.send(cmd)
        
    def received_message(self, m):
        print m
        self.close()

if __name__ == '__main__':
    import optparse
    
    parser = optparse.OptionParser()
    parser.add_option("-H", "--host", dest="hostname", default='127.0.0.1',
                      help="Connect to volumed server using specified port")
    parser.add_option("-p", "--port", type=int, dest="port", default=8888,
                      help="Connect to volumed server using specified port")
    parser.add_option("-c", "--command",  dest="command",
                      help="Execute the specified volumed command")

    (options, args) = parser.parse_args()

    if len(args) != 0:
        sys.stderr.write("Unexpected args: %s\n" % " ".join(args))
        sys.exit(2)

    try:
        ws = VolumeClient('ws://%s:%d' % (options.hostname, options.port),
                         protocols=['http-only', 'chat'])
        ws.connect()
        ws.sendcmd("%s\n" % options.command)
        ws.run_forever()
    except KeyboardInterrupt:
        ws.close()
