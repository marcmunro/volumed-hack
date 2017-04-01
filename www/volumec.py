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
# Finally, volumec can run as a daemon wherein it acts as an lirc client
# so that volume can be managed from infra-red remotes.
#

import sys
sys.path.append('/usr/local/lib/python2.7/site-packages')
from ws4py.client.threadedclient import WebSocketClient
import threading

class VolumeClient(WebSocketClient):
    def __init__(self, instream, options, *args, **kwargs):
        super(VolumeClient, self).__init__(*args, **kwargs)
        self.instream = instream
        self.options = options
        self.expecting_response = False
        self._close_after_msg = False

    def closed(self, code, reason=None):
        if code != 1000:
            print "Closed down", code, reason

    def close_after_msg(self):
        if self.expecting_response:
            self._close_after_msg = True
        else:
            self.close()
            
    def write(self, cmd):
        self.sendcmd(cmd)
        
    def sendcmd(self, cmd):
        self.expecting_response = True
        if self.options.verbose:
            print "TX: %s" % cmd.strip()
        self.send(cmd)
        
    def received_message(self, m):
        if not self.options.quiet:
            print m.data.strip()
        if self._close_after_msg:
            self.close()
        self.expecting_response = False


class StreamTermination(Exception): pass

class CommandStream:
    def __init__(self, cmd):
        self.cmd = cmd

    def close(self): pass
    
    def readline(self):
        cmd = self.cmd
        self.cmd = None
        return cmd

class IRCmdStream:
    """Provide a stream-like interface to the lirc subsystem.  This
    allows us to fetch commands from infra-red controllers.

    NOTE: There must be only one instance of this class as we rather
    abuse the lirc.init() and lirc.deinit() functions."""

    def __init__(self, name):
        import lirc
        self.name = name
        self.lirc = lirc
        self.sockid = lirc.init(name)

    def __del__(self):
        self.lirc.deinit()

    def close(self):
        self.lirc.deinit()
        
    def readline(self):
        while True:
            try:
                list = self.lirc.nextcode()
            except Exception:
                raise StreamTermination(
                    "volumec: Disconnected from lirc socket\n")
            str = " ".join(list).strip()
            if str != '':
                return str


class FileStream:
    def __init__(self, filename, options):
        self.filename = filename
        self.options = options
        if options.hold:
            import stat, os
            self.reopen = stat.S_ISFIFO(os.stat(options.file).st_mode)
        else:
            self.reopen = False
        self.stream = open(options.file, "r")

    def close(self):
        self.stream.close()

    def readline(self):
        while True:
            line = self.stream.readline
            if line:
                if (line == 'q\n') or (line == 'quit\n'):
                    return None
                return line
            if self.reopen:
                self.stream = open(options.file, "r")

    
if __name__ == '__main__':
    import optparse
    import signal
    
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
    parser.add_option("-d", "--lirc",  "--daemon", dest="daemon",
                      action="store_true",
                      help="Run as an lirc client (daemon)")
    parser.add_option("-v", "--verbose",  dest="verbose", action="store_true",
                      help="Provide verbose output")
    parser.add_option("-q", "--quiet",  dest="quiet", action="store_true",
                      help="Do not print responses from volumed")

    (options, args) = parser.parse_args()

    if len(args) != 0:
        sys.stderr.write("Unexpected args: %s\n" % " ".join(args))
        sys.exit(2)

    instream = None
    if options.command:
        # TODO: Check for conflicting options
        instream = CommandStream(options.command)
    elif options.daemon:
        # TODO: Check for conflicting options
        try:
            instream = IRCmdStream("volumec")
        except Exception as e:
            sys.stderr.write(
                ("volumec: Unable to connect with lirc daemon.\n    %s\n" +
                 "Closing down.\n") % str(e))
            sys.exit(2)
        
    elif options.file:
        # TODO: Check for conflicting options
        instream = open(options.file, "r")
    else:
        instream = sys.stdin

    sighup_received = False
    sigterm_received = False
    exitcode = 0
    
    def handleHup(signum, frame):
        print 'SIGHUP received: taking no action...'
        global sighup_received
        sighup_received = True

    def handleTerm(signum, frame):
        print 'SIGTERM received: closing down...'
        global sigterm_received
        sigterm_received = True
        instream.close()

    signal.signal(signal.SIGHUP, handleHup)
    signal.signal(signal.SIGTERM, handleTerm)
        
    ws = VolumeClient(instream, options,
                      'ws://%s:%d' % (options.hostname, options.port),
                      protocols=['http-only', 'chat'])

    try:
        ws.connect()
    except Exception as e:
        sys.stderr.write(
            ("volumec: Unable to connect with volumed.\n    %s\n" +
             "Closing down.\n") % str(e))
        sys.exit(2)
        
    try:
        while True:
            try:
                msg = instream.readline()
            except StreamTermination:
                sys.stderr.write(
                    "volumec: Disconnected from lirc socket\n")

            except Exception as e:
                if sighup_received:
                    # Just try again
                    sighup_received = False
                    continue
                ws.close()
                if sigterm_received:
                    break
                sys.stderr.write(
                    "volumec: Error on input\n    %s\n" % str(e))
                os.exit(2)
            if msg:
                try:
                    ws.write(msg.strip() + "\n")
                except Exception:
                    sys.stderr.write(
                        "volumec: Lost contact with volumed.  Closing down.\n")
                    ws.close
                    sys.exit(2)
            else:
                break

        ws.close_after_msg()
        ws.run_forever()
    except KeyboardInterrupt:
        ws.close()
        sys.exit(1)

