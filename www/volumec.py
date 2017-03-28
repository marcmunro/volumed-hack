#! /usr/bin/env python
#
# This Program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License, version 3, as
# published by the Free Software Foundation.
# 
# This Program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
# 
# You should have received a copy of the GNU General Public License
# along with TsunAMP; see the file COPYING.  If not, see
# <http://www.gnu.org/licenses/>.
# 
# Volume Control Daemon   (c) 2017 Marc Munro
#
# Built for Moode audio player.
#
# This provides a websocket client for communicating with the volumed
# service.  It either takes commands on the command line, or from an
# input stream.  If the input stream is a named pipe, it can be
# continually re-opened so that commands may simply be echoed to it.
# This last feature is primarily for use for IR remote controls which
# can use simple echo operations to pass on their actions.  This should
# make the remotes much more responsive as commands will not end up
# being queued while previous commands complete their execution.
#

import sys
sys.path.append('/usr/local/lib/python2.7/site-packages')
from ws4py.client.threadedclient import WebSocketClient

class VolumeClient(WebSocketClient):
    def __init__(self, *args, **kwargs):
        super(VolumeClient, self).__init__(*args, **kwargs)
        self.more_expected = True
        self.expecting_response = False
        
    def closed(self, code, reason=None):
        if code != 1000:
            print "Closed down", code, reason

    def close_after_msg(self):
        if self.expecting_response:
            self.more_expected = False
        else:
            self.close()
            
    def sendcmd(self, cmd):
        self.expecting_response = True
        self.send(cmd)
        
    def received_message(self, m):
        print m.data.strip()
        if not self.more_expected:
            self.close()
        self.expecting_response = False

if __name__ == '__main__':
    import optparse
    
    parser = optparse.OptionParser()
    parser.add_option("-H", "--host", dest="hostname", default='127.0.0.1',
                      help="Connect to volumed server using specified port")
    parser.add_option("-p", "--port", type=int, dest="port", default=8888,
                      help="Connect to volumed server using specified port")
    parser.add_option("-c", "--command",  dest="command",
                      help="Execute the specified volumed command")
    parser.add_option("-f", "--file",  dest="file",
                      help="Read commands from file (use - for stdin)")
    parser.add_option("-o", "--hold",  dest="hold", action="store_true",
                      help="If file is a pipe, hold it open continuously")

    (options, args) = parser.parse_args()

    if len(args) != 0:
        sys.stderr.write("Unexpected args: %s\n" % " ".join(args))
        sys.exit(2)

    try:
        ws = VolumeClient('ws://%s:%d' % (options.hostname, options.port),
                         protocols=['http-only', 'chat'])
        ws.connect()
        reopen = False
        
        if options.command:
            ws.sendcmd("%s\n" % options.command)
            ws.close_after_msg()
            ws.run_forever()
            sys.exit(0)

        if options.file:
            import stat, os
            wstream = None
            if stat.S_ISFIFO(os.stat(options.file).st_mode):
                reopen = options.hold
            stream = open(options.file, "r")
        else:
            stream = sys.stdin
        while True:
            line = stream.readline()
            if line:
                if (line == 'q\n') or (line == 'quit\n'):
                    ws.close()
                    stream.close()
                    break
                ws.sendcmd(line)
            else:
                # EOF detected
                if reopen:
                    stream = open(options.file, "r")
                else:
                    ws.close_after_msg()
                    break
        if options.file:
            stream.close()
                
    except KeyboardInterrupt:
        ws.close()

    ws.run_forever()
