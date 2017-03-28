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
# This provides a websocket interface for controlling the volume and
# mute, which can greatly improve the responsiveness of a web client.
# It also manages the update of Moode's database for volume and mute
# status, and actively polls the hardware so that any out of band change
# to volume/mute can be reported to clients.
#
# Note that a client, volumec.py, is also provided for testing purposes
# and for implementing shell-based interfaces, eg for IR remotes.
#
# TODO:
#   - volume curve correction: see note in
#       VolumeController.correct_volume()
#

import sys
sys.path.append('/usr/local/lib/python2.7/site-packages')
from gevent import monkey; monkey.patch_all()
from ws4py.websocket import WebSocket
from ws4py.server.geventserver import WSGIServer
from ws4py.server.wsgiutils import WebSocketWSGIApplication

import threading
import time
import sys
import re
import Queue
import sqlite3
import subprocess

from ws4py import configure_logger
configure_logger()

DEBUG = False		# Global flag for controlling debug output.

class Singleton(object):
  _instances = {}
  def __new__(class_, *args, **kwargs):
    if class_ not in class_._instances:
        class_._instances[class_] = super(Singleton, class_).__new__(
            class_, *args, **kwargs)
    return class_._instances[class_]


class ThreadPlus(threading.Thread):
    """Thread with added stop, sleep and sleep-target manipulation
    methods."""
    RESOLUTION = 0.1

    def __init__(self):
        super(ThreadPlus, self).__init__()
        self.running = True
        self.sleep_target = 0
        self.target_lock = threading.Lock()

    def stop(self):
        self.running = False

    def set_sleep_target(self, target_time):
        with self.target_lock:
            self.sleep_target = target_time
        
    def target(self):
        with self.target_lock:
            return self.sleep_target
        
    def sleep(self, sleep_time):
        """Sleep for sleep_time seconds or until the thread is stopped.
        Return True if we are still running (ie we reached our timeout).
        Note that the timeout may have been modified while we slept.  If
        so, we will only return True if we reach the modified timeout."""
        now = time.time()
        self.set_sleep_target(now + sleep_time)
        
        while self.running:
            # sys.stdout.flush() # Uncomment when tee-ing the output for debug
            tick = min(now + ThreadPlus.RESOLUTION, self.target())
            time.sleep(tick - now)
            now = time.time()
            if now >= self.target():
                return True
            
class HWInterface:
    """Provide an interface to the volume control hardware.  This also
    records the volume and mute settings in the database."""

    def __init__(self, db):
        self.db = db
        self.volume_re = re.compile(
          "([0-9]*)?[^0-9]*([0-9]+)%(.*\[(on|off)\])?")
        self.cardnum = self.get_cardnum()
        
    def get_cardnum(self):
        """Based on vol.sh, though I am not entirely convinced.  My use
        case for the music box includes having a usb audio capture
        device.  I fear that such an extra card may make this approach
        fail - I  guess we'll see."""
        try:
            open("/proc/asound/card1/id")
            return 1
        except IOError:
            return 0
        
    def get_volume(self):
        if self.db.mpd_mixer == 'hardware':
            if self.db.volcurve == 'Yes':
                cmd = ("amixer -c %d sget %s -M" %
                       (self.cardnum, self.db.alsa_mixer))
            else:
                cmd = ("amixer -c %d sget %s" %
                       (self.cardnum, self.db.alsa_mixer))
        else:
            cmd = "mpc"

        # TODO: Put in some error handling here    
        out = subprocess.check_output(cmd.split(' '))
        match = self.volume_re.search(out)
        vol = int(match.group(2))
        if self.db.mpd_mixer == 'hardware':
            mute = match.group(4) == 'off'
        else:
            mute = (vol == 0) and (self.db.mute == 'True')
            vol = self.db.level
        return vol, mute
    
    def set_mute(self, mute=True):
        if self.db.mpd_mixer == 'hardware':
            cmd = ("amixer -c %d sset %s %s" %
                   (self.cardnum, self.db.alsa_mixer,
                    'mute' if mute else 'unmute'))
            out = subprocess.check_output(cmd.split(' '))
        else:
            # We think we do not have a h/w mute as we must use mpc
            if mute:
                self.set_volume(0)
            else:
                self.set_volume(self.db.level)
        
    def set_volume(self, volume):
        if self.db.mpd_mixer == 'hardware':
            if self.db.volcurve == 'Yes':
              cmd = ("amixer -c %d sset %s -M% d%%" %
                       (self.cardnum, self.db.alsa_mixer, volume))
            else:
                cmd = ("amixer -c %d sset %s %d%%" %
                       (self.cardnum, self.db.alsa_mixer, volume))
        else:
            cmd = "mpc volume %s" % volume

        # TODO: log the following?
        out = subprocess.check_output(cmd.split(' '))
        match = self.volume_re.search(out)
        result = int(match.group(2))
        if result != volume:
            # We have a discrepency between what we requested and what
            # we got back.  This is probably due to rounding errors in
            # the pct calculation, so let's try to overcome them.
            if match.group(1):
                # We have an actual value as well as a pct.  Let's
                # try incrementing or decrementing it.
                actual = int(match.group(1))
                if result < volume:
                    actual += 1
                else:
                    actual -= 1
                if self.db.mpd_mixer == 'hardware':
                    cmd = ("amixer -c %d sset %s %d" %
                           (self.cardnum, self.db.alsa_mixer, actual))
                    out = subprocess.check_output(cmd.split(' '))


class DB:
    """Provide a nice simple setter/getter interface to the database
    fields."""
    
    STALE_LIMIT = 2.0
    FIELD_IDS = {'volcurve': 32,
                 'volcurvefac': 56,
                 'max_pct': 34,
                 'level': 35,
                 'mute': 36,
                 'warning_level': 37,
                 'alsa_mixer': 39,
                 'mpd_mixer': 40}
    
    def __init__(self, dbname):
        self.dbname = dbname
        self.connection = sqlite3.connect(self.dbname)
        self.fields = {}
        self.fetchtimes = {}
        for field in DB.FIELD_IDS:
            self.fields[field] = None
            self.fetchtimes[field] = 0
        
    def fetch(self, field):
        now = time.time()    
        if self.fetchtimes[field] + DB.STALE_LIMIT < now:
            # We do not have an up-to-date value for the field, so we
            # will fetch it.  This time-based approach allows us to use
            # our database fields as simple attributes of the DB object
            # without having to be concerned about the cost of fetches:
            # we will fetch from the database when the local copy is
            # stale and use our cached version otherwise.
            qry = ("select value from cfg_engine where id = %d" %
                   DB.FIELD_IDS[field])
            c = self.connection.cursor()
            c.execute(qry)
            res = c.fetchall()
            self.fetchtimes[field] = now
            self.fields[field] = res[0][0]
        return self.fields[field]

    def update(self, field, value):
        if value != self.fetch(field):
            # Only update the database if the value is known to have
            # changed.
            qry = ("update cfg_engine set value = '%s' where id = %d" %
                   (value, DB.FIELD_IDS[field]))
            c = self.connection.cursor()
            c.execute(qry)
            self.fields[field] = value
            self.connection.commit()
            #res = c.fetchall()
            self.fetchtimes[field] = time.time()
        
    def __getattr__(self, name):
        return self.fetch(name)
    
    def __setattr__(self, name, value):
        if name in DB.FIELD_IDS:
            self.update(name, value)
        else:
            self.__dict__[name] = value

            
class VolumeMonitor(ThreadPlus):
    RESOLUTION = 2.0

    def __init__(self, controller):
        super(VolumeMonitor, self).__init__()
        self.controller = controller
        self.volume, self.mute = self.controller.get_volume()
        self.start()

    def report_change(self):
        volume, mute = self.controller.get_volume()
        if (volume != self.volume) or (mute != self.mute):
            self.volume, self.mute = volume, mute
            self.controller.update_watchers(volume, mute)

    def trigger_recheck(self):
        self.set_sleep_target(time.now())
            
    def run(self):
        while self.running:
            if self.sleep(VolumeMonitor.RESOLUTION):
                self.report_change()
            

class Termination(Exception): pass
    
class VolumeController(ThreadPlus):
    def __init__(self, dirname, options):
        super(VolumeController, self).__init__()
        self.running = True
        self.emulate = options.emulate
        self.db = DB("%s/db/player.db" % dirname)
        self.hw_interface = HWInterface(self.db)
        self.monitor = None if self.emulate else VolumeMonitor(self)
        self.queue = Queue.Queue()
        self.volume_re = re.compile("^ *vol *([+-])? *([0-9]+)? *$",
                                    re.IGNORECASE)
        self.mute_re = re.compile("^ *(Un)?Mute *$", re.IGNORECASE)
        self.quit_re = re.compile("^ *q(uit)? *$", re.IGNORECASE)
        self.watch_re = re.compile("^ *watch *$", re.IGNORECASE)
        #self.shutdown_re = re.compile("^ *shutdown *$", re.IGNORECASE)
        self.watchers = {}
        self.watcher_lock = threading.Lock()
        self.start()

    def parse_message(self, message):
        match = self.volume_re.match(message)
        cmd, val = None, None
        if match:
            if match.group(2):  # ie, we have digits
                val = int(match.group(2))
            if match.group(1):  # we have + or -
                cmd = 'delta'
                if match.group(1) == '-':
                    val = -val
            else:
                # No + or -
                if match.group(2):
                    cmd = 'set'
                else:
                    cmd = 'get'
        else:
            match = self.mute_re.match(message)
            if match:
                val = 0
                if match.group(1):
                    cmd = 'unmute'
                else:
                    cmd = 'mute'
            else:
                if self.quit_re.match(message):
                    cmd = 'quit'
                elif self.watch_re.match(message):
                    cmd = 'watch'
                #elif self.shutdown_re.match(message):
                #    cmd = 'shutdown'
                    
        if DEBUG:
            print ("PARSED CMD: %s, VAL: %s (message: \"%s\")" %
                   (cmd, val, message))
        return (cmd, val)
        
    def process_message(self, socket, message):
        if DEBUG:
            print "PROCESSING MSG: \"%s\"" % message
        cmd, val = self.parse_message(message)
        self.queue.put((socket, cmd, val, message))
            
    def get(self, block=True, timeout=None):
        # Safe version of get.
        if block and timeout is None:
            while self.running:
                try:
                    res = self.queue.get(True, ThreadPlus.RESOLUTION)
                    return res
                except Queue.Empty:
                    pass
        else:
            res = self.queue.get(block, timeout)
            return res
        
    def get_requests(self):
        """Compile all outstanding requests into a single list to
        process.  Each list entry is a tuple of the form: (conduit,
        request_string)"""
        request = self.get()
        if request:
            requests = [request]
            while not self.queue.empty():
                requests.append(self.get(False))
            return requests

    def add_socket(self, current, socket):
        if socket in current:
            current[socket] += 1
        else:
            current[socket] = 1
        return current

    def send(self, sockets, msg):
        for socket in sockets:
            if msg and self.running:
                if DEBUG:
                    print "SENDING MESSAGE: \"%s\"" % msg
                try:
                    socket.send(msg)
                except Exception:
                    # Assume the socket was closed, not much we can do.
                    sys.stderr.write("Send failed.  Msg: \"%s\".\n" %
                                     msg.strip())
                    try:
                        socket.close()
                    except Exception:
                        pass
            else:
                socket.close()

    def report_change(self):
        if self.monitor:
            self.monitor.report_change()
                
    def set_mute(self, mute=True):
        if not self.emulate:
            self.hw_interface.set_mute(mute)
        self.db.mute = 'True' if mute else 'False'
        self.report_change()

    def correct_volume(self, vol, writing):
        """Stub for doing volume curve correction.  Currently this does
        nothing.

        Note that it *must* work so that: 
          self.correct_volume(self.correct_volume(vol, False), True))
        is equal to:
          vol
        and also to: 
          self.correct_volume(self.correct_volume(vol, True), False))

        Note that when reading and writing volume data from/to the
        hardware interface, it will probably be better to read/write the
        absolute volume level rather than using percentages.  No
        provision has currently been made to do this.  You will need to
        modify the `cmd' variables in HWInterface.get_volume() and
        HWInterface.set_volume() in order to do this."""
        return vol
        
    def get_volume(self):
        if not self.emulate:
            vol, mute = self.hw_interface.get_volume()
            self.db.level = self.correct_volume(vol, False)
            self.db.mute = 'True' if mute else 'False'
        return self.db.level, self.db.mute == 'True'

    def set_volume(self, vol):
        max_pct = int(self.db.max_pct)

        if vol < 0:
            vol = 0
        elif vol > max_pct:
            vol = max_pct

        vol = self.correct_volume(vol, True)

        if not self.emulate:
            self.hw_interface.set_volume(vol)
        self.db.level = vol
        self.report_change()

    def compose_response(self, vol, mute):
        return "Vol: %s, Mute: %s\n" % (vol, 'on' if mute else 'off')
        
    def send_responses(self, sockets):
        self.send(sockets,
                  self.compose_response(self.db.level, self.db.mute == 'True'))
                
    def process_requests(self, requests):
        vol = int(self.db.level)
        set = False
        setters = {}
        get = False
        getters = {}
        mute = False
        muters = {}
        unmute = False
        unmuters = {}
        quit = False
        quitters = {}
        for socket, cmd, val, msg in requests:
            if DEBUG:
                print "PROCESSING CMD: \"%s\" (VAL: %s)" % (cmd, val)
            if cmd == 'get':
                get = True
                getters = self.add_socket(getters, socket)
            elif cmd == 'set':
                set = True
                vol = val
                setters = self.add_socket(setters, socket)
            elif cmd == 'delta':
                set = True
                vol += val
                setters = self.add_socket(setters, socket)
            elif cmd == 'mute':
                mute = True 
                unmute = False
                muters = self.add_socket(muters, socket)
            elif cmd == 'unmute':
                unmute = True 
                mute = False
                unmuters = self.add_socket(unmuters, socket)
            elif cmd == 'quit':
                quit = True
                quitters = self.add_socket(quitters, socket)
            elif cmd == 'watch':
                with self.watcher_lock:
                    self.watchers = self.add_socket(self.watchers, socket)
            else:
                socket.send("Unknown command: \"%s\"" % msg)
        if mute:
            self.set_mute(True)
            self.send_responses(muters)
        if set:
            self.set_volume(vol)
            self.send_responses(setters)
        if unmute:
            self.set_mute(False)
            self.send_responses(unmuters)
        if get:
            self.get_volume()
            self.send_responses(getters)
        if quit:
            self.send(quitters, None)

    def update_watchers(self, vol, mute):
        with self.watcher_lock:
            watchers = self.watchers

        msg = self.compose_response(vol, mute)
        ok_watchers = {}
        for w in watchers:
            try:
                w.send(msg)
                ok_watchers[w] = 1
            except Exception as e:
                try:
                    w.close()
                except Exception:
                    pass
        with self.watcher_lock:
            self.watchers = ok_watchers
        
    def run(self):
        # This is where we asynchronously parse and process commands
        # read from our input stream.  Note that although this is
        # asynchronous with regard to the input stream, the command
        # stream is single-threaded.
        try:
            while self.running:
                requests = self.get_requests()
                if requests and self.running:
                    #print "REQUESTS: %s" % (requests,)
                    self.process_requests(requests)
        except Termination:
            print "TERMINATING"
        
        if self.monitor:
            self.monitor.stop()
            self.monitor.join()

class SingleVolumeController(Singleton):
    """This creates a Singleton instance of the VolumeServer class.
    There is probably a more elegant way of doing this but this appears
    to work.  We need this because we want to serialise our access to
    both the database and hardware, and each VolumeServer instance needs
    to be able to find this."""
    def __init__(self, *args):
        if not '_vc' in self.__dict__:
            self._vc = VolumeController(*args)

    def __getattr__(self, name):
        if name != '_vc':
            return self._vc.__getattribute__(name)
            
    def __setattr__(self, name, value):
        if name == '_vc':
            self.__dict__[name] = value
        else:
            self._vc.__dict__[name] = value
            

class VolumeServer(WebSocket):
    def __init__(self, *args, **kwargs):
        super(VolumeServer, self).__init__(*args, **kwargs)
        self.vc = SingleVolumeController()
        
    def received_message(self, message):
        if not message.is_binary:
            self.vc.process_message(self, message.data.strip())


if __name__ == '__main__':
    import optparse
    import os
    import signal

    parser = optparse.OptionParser()
    parser.add_option(
        "-p", "--port", type=int, dest="port", default=8888,
        help="Run volumed server using specified port (default 888)")
    parser.add_option("-e", "--emulate",  dest="emulate",
                      action="store_true", help="Emulate the hw interface")
    parser.add_option("-d", "--debug",  dest="debug", action="store_true",
                      help="Provide some debugging output")

    (options, args) = parser.parse_args()
    DEBUG = options.debug
    dirname = os.path.dirname(sys.argv[0])
    controller = SingleVolumeController(dirname, options)

    try:
        server = WSGIServer(('', 8888),
                            WebSocketWSGIApplication(handler_cls=VolumeServer))
        def handleHup(signum, frame):
            print 'SIGHUP received: taking no action...'

        def handleTerm(signum, frame):
            print 'SIGTERM received: closing down...'
            server.stop()

        signal.signal(signal.SIGHUP, handleHup)
        signal.signal(signal.SIGTERM, handleTerm)
    
        server.serve_forever()
    except KeyboardInterrupt: pass
    except Termination: pass
    controller.stop()
    controller.join()
