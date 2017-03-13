#! /usr/bin/env python
#
# This Program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3, or (at your option)
# any later version.
#
# This Program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Moode; see the file COPYING.  If not, see
# <http://www.gnu.org/licenses/>.
#
# Responsive volume manager for Moode Audio Player.
#
# (C) Marc Munro 2017
#

# TODO:
# Change protocol to remove length byte and instead use \0 message
# terminator?
# Provide a websocket mechanism to this
# Update moode to use this
# Move handling of logarithmic volume control into here from javascript
# Modify javascript to be more responsive
# daemonify

import sys
import os
import threading
import time
import Queue
import re
import sqlite3
import subprocess

class ThreadPlus(threading.Thread):
    """Thread with added stop, sleep and sleep_target manipulation
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
            
class VolumeController(ThreadPlus):
    TIMEOUT = 2.0

    """Class to process control messages for the the volume control."""
    def __init__(self, monitor, hw_interface, db):
        super(VolumeController, self).__init__()
        self.monitor = monitor
        self.hw_interface = hw_interface
        self.db = db
        self.queue = Queue.Queue()
        self.volume_re = re.compile("^ *vol *([+-])? *([0-9]+) *$",
                                    re.IGNORECASE)
        self.mute_re = re.compile("^ *(Un)?Mute *$", re.IGNORECASE)
        self.quit_re = re.compile("^ *q(uit)? *$", re.IGNORECASE)
        self.shutdown_re = re.compile("^ *shutdown *$", re.IGNORECASE)
        self.start()

    def stop(self):
        super(VolumeController, self).stop()
        self.monitor.stop()
        self.db.stop()
        self.db.join()
        self.monitor.stop()
        
    def put(self, payload):
        #print "Put: %s\n" % (payload,)
        self.queue.put(payload)
        
    def get(self, block=True, timeout=None):
        # Safe version of get.  TODO: refactor this into ThreadPlus
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
        
    def parse_request(self, request):
        match = self.volume_re.match(request)
        cmd, val = None, None
        if match:
            if match.group(2):
                val = int(match.group(2))
            if match.group(1):
                cmd = 'delta'
                if match.group(1) == '-':
                    val = -val
            else:
                if match.group(2):
                    if match.group(3):
                        cmd = 'set'
                else:
                    cmd = 'get'
        else:
            match = self.mute_re.match(request)
            if match:
                val = 0
                if match.group(1):
                    cmd = 'unmute'
                else:
                    cmd = 'mute'
            else:
                if self.quit_re.match(request):
                    cmd = 'quit'
                elif self.shutdown_re.match(request):
                    cmd = 'shutdown'
                    
        print "CMD: %s, VAL: %s (request: \"%s\")" % (cmd, val, request)
        return (cmd, val)

    def set_mute(self):
        self.monitor.reset_wait()   # No point in fetching current volume any
                                    # time soon.
        self.monitor.record_muted(True)
        if self.db.mpd_mixer == 'hardware':
            self.hw_interface.set_mute()
        else:
            self.hw_interface.set_volume(0)
    
    def set_unmute(self):
        self.monitor.reset_wait()   # No point in fetching current volume any
                                    # time soon.
        level = int(self.db.level)
        if self.db.mpd_mixer == 'hardware':
            self.hw_interface.set_mute(False)
        else:
            self.hw_interface.set_volume(level)
        self.monitor.record_muted(False)
    
    def set_volume(self, vol):
        self.monitor.reset_wait()   # No point in fetching current volume any
                                    # time soon.
        warn_level = int(self.db.warning_level)
            
        if vol < 0:
            vol = 0
        elif vol > warn_level:
            vol = warn_level
        if (self.db.mute == '0') or (self.db.mpd_mixer == 'hardware'):
            self.hw_interface.set_volume(vol)
            self.monitor.record_volume(vol)

    def add_conduit(self, current, conduit):
        if conduit in current:
            current[conduit] += 1
        else:
            current[conduit] = 1
        return current

    def send(self, conduits, msg):
        for conduit in conduits:
            if msg and self.running:
                conduit.write(msg)
            else:
                conduit.close()

    def send_response(self, conduits):
        self.send(conduits, "Vol: %d, Mute: %s\n" % (
            self.monitor.last_volume,
            'on' if self.monitor.last_muted else 'off'))
                
    def process_requests(self, requests):
        """This combines a stream of possibly related commands in requests
        into grouped get and/or set operations.  This means that a
        fast stream of vol +1 commands will likely be concatenated
        into a single vol +N command.  The point of this is to improve
        responsiveness to things such as remote controls."""

        self.monitor.reset_wait()   # No point in fetching current volume any
                                    # time soon.
        vol = self.monitor.volume()
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
        for conduit, request in requests:
            cmd, val = self.parse_request(request)
            if cmd:
                if cmd == 'get':
                    get = True
                    getters = self.add_conduit(getters, conduit)
                elif cmd == 'set':
                    set = True
                    vol = val
                    setters = self.add_conduit(setters, conduit)
                elif cmd == 'delta':
                    set = True
                    vol += val
                    setters = self.add_conduit(setters, conduit)
                elif cmd == 'mute':
                    mute = True 
                    unmute = False
                    muters = self.add_conduit(muters, conduit)
                elif cmd == 'unmute':
                    unmute = True 
                    mute = False
                    unmuters = self.add_conduit(unmuters, conduit)
                elif cmd == 'quit':
                    quit = True
                    quitters = self.add_conduit(quitters, conduit)
                elif cmd == 'shutdown':
                    conduit.shutdown()
                    self.stop()
                    return
            else:
                conduit.write("Unable to parse msg: \"%s\"\n" % request)
        if self.running:
            if mute:
                self.set_mute()
                self.send_response(muters)
            if set:
                self.set_volume(vol)
                self.send_response(setters)
            if unmute:
                self.set_unmute()
                self.send_response(unmuters)
            if get:
                self.send_response(getters)
            if quit:
                self.send(quitters, None)
    
    def get_requests(self):
        """Compile all outstanding requests into a single list to
        process.  Each list entry is a tuple of the form: (conduit,
        request_string)"""
        requests = self.get()
        if requests:
            requests = [requests]
            while not self.queue.empty():
                requests.append(self.get(False))
        return requests

    def run(self):
        # This is where we asynchronously parse and process commands
        # read from our input stream.  Note that although this is
        # asynchronous with regard to the input stream it is synchronous
        # with regard to our command stream.  IE, we only process the
        # queue once any previous command has been completely processed.
        while self.running:
            requests = self.get_requests()
            if requests and self.running:
                self.process_requests(requests)
            
class Monitor(ThreadPlus):
    RESOLUTION = 2.0
    
    """Class to provide periodic monitoring of volume control.  
    This allows us to discover volume changes caused by events outside of
    our control."""

    def __init__(self, hw_interface, db):
        super(Monitor, self).__init__()
        self.hw_interface = hw_interface
        self.db = db
        self.last_volume, self.last_muted = self.fetch_volume()
        self.volume_lock = threading.Lock()
        self.wake_time = 0
        self.start()

    def fetch_volume(self):
        return self.hw_interface.volume()
        
    def volume(self):
        return self.last_volume
        
    def record_muted(self, muted):
        self.record_volume(self.last_volume, muted)

    def record_volume(self, volume, muted=None):
        """Record the volume if it has changed.  Note that this may
        safely  be called asynchronously."""
        if (volume == 0) and (self.db.mute == '1'):
            return # Do not update our record of the desired volume

        with self.volume_lock:
            if self.last_volume != volume:
                self.last_volume = volume
                update_vol = True
            else:
                update_vol = False
            if muted is None:
                update_mute = False
            else:
                if self.last_muted != muted:
                    self.last_muted = muted
                    update_mute = True
                else:
                    update_mute = False
        # Order is important (we mute before altering volume, and
        # alter volume before unmuting
        if update_mute and muted:
            self.db.mute = '1'
        if update_vol:
            self.db.level = volume
        if update_mute and not muted:
            self.db.mute = '0'
        
    def reset_wait(self):
        """Reset our sleep timeout time."""
        self.set_sleep_target(time.time() + Monitor.RESOLUTION)

    def run(self):
        while self.running:
            if self.sleep(Monitor.RESOLUTION):
                vol, mute = self.fetch_volume()
                self.record_volume(vol, mute)
                
class HWInterface:
    """Provide an interface to the volume control hardware."""

    def __init__(self, db, emulate=False):
        self.db = db
        self.volume_re = re.compile("([0-9]+)?[^0-9]*([0-9]+)%.*\[(on|off)\]")
        self.cardnum = self.get_cardnum()
        self.emulation = (42, False) if emulate else None
        
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
        
    def volume(self):
        if self.emulation:
            return self.emulation
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
            mute = match.group(3) == 'off'
        else:
            mute = self.db.mute == '1'
        return vol, mute
    
    def set_mute(self, mute=True):
        if self.emulation:
            self.emulation = (self.emulation[0], mute)
            return self.emulation
        if self.db.mpd_mixer == 'hardware':
            cmd = ("amixer -c %d sset %s %s" %
                   (self.cardnum, self.db.alsa_mixer,
                    'mute' if mute else 'unmute'))
            out = subprocess.check_output(cmd.split(' '))
        
    def set_volume(self, volume):
        if self.emulation:
            self.emulation = (volume, self.emulation[1])
            return self.emulation
            return self.emulation
        if self.db.mpd_mixer == 'hardware':
            if self.db.volcurve == 'Yes':
                cmd = ("amixer -c %d sset %s -M% d%%" %
                       (self.cardnum, self.db.alsa_mixer, volume))
            else:
                cmd = ("amixer -c %d sset %s %d%%" %
                       (self.cardnum, self.db.alsa_mixer, volume))
        else:
            cmd = "mpc volume %d" % volume

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
                    
class NoDBException(Exception): pass

class DB(ThreadPlus):
    """Provide a nice simple setter/getter interface to the database
    fields, and allow it to be done with threads."""
    
    STALE_LIMIT = 1.0
    FIELD_IDS = {'volcurve': 32,
                 'max_pct': 34,
                 'level': 35,
                 'mute': 36,
                 'warning_level': 37,
                 'alsa_mixer': 39,
                 'mpd_mixer': 40}
    
    def __init__(self, dbname):
        super(DB, self).__init__()
        self.dbname = dbname
        self.fields = {}
        self.fetchtimes = {}
        self.qry_q = Queue.Queue()
        self.res_q = Queue.Queue()
        self.start()

    def stop(self):
        super(DB, self).stop()
        self.qry_q.put("q")	# Cause our thread to stop
        
    def run(self):
        """This is our main thread and exists in order to allow *any*
        thread to access the database.  All database accesses are done
        through queues, and are serialised through the main thread in
        this object.  Callers must ensure they do not hold locks when
        accessing the database or deadlocks may become possible."""
        connection = sqlite3.connect(self.dbname)
        while self.running:
            qry = self.qry_q.get()
            if qry != "q":	# Handle the thread stop command
                c = connection.cursor()
                c.execute(qry)
                self.res_q.put(c.fetchall())
                
    def makeField(self, field):
        if not field in self.fields:
            if not field in DB.FIELD_IDS:
                raise AttributeError("No such attribute: %s" % field)
            self.fields[field] = None
            self.fetchtimes[field] = 0
        
    def fetch(self, field):
        if not self.running:
            raise NoDBException()

        self.makeField(field)
        now = time.time()    
        if self.fetchtimes[field] + DB.STALE_LIMIT < now:
            # We do not have an up-to-date value for the field, so we
            # will fetch it.  This time-based approach allows us to use
            # our database fields as simple attributes of the DB object
            # without having to be concerned about the cost of fetches:
            # we will fetch from the database when the local copy is
            # stale and use our cached version otherwise.
            self.qry_q.put("select value from cfg_engine where id = %d" %
                      DB.FIELD_IDS[field])
            res = self.res_q.get()
            self.fetchtimes[field] = now
            self.fields[field] = res[0][0]
        return self.fields[field]

    def update(self, field, value):
        if not self.running:
            raise NoDBException()
        self.makeField(field)
        #print "DB: set %s to %s" % (field, value)
        self.qry_q.put("update cfg_engine set value = '%s' where id = %d" %
                       (value, DB.FIELD_IDS[field]))
        res = self.res_q.get()  # for each qry_q write there must be a
    		                # matching res_q read!
        self.fetchtimes[field] = time.time()
        self.fields[field] = value
        
    def __getattr__(self, name):
        return self.fetch(name)
    
    def __setattr__(self, name, value):
        if name in DB.FIELD_IDS:
            self.update(name, value)
        else:
            self.__dict__[name] = value


class Interlocutor(threading.Thread):
    """Receive requests from a connected socket and provide responses."""
    def __init__(self, socket, controller, serversocket, deaded):
        super(Interlocutor, self).__init__()
        self.socket = socket
        self.controller = controller
        self.serversocket = serversocket
        self.deaded = deaded
        self.running = True
        self.buffered = ''
        self.start()

    def send(self, msg):
        if self.running:
            totalsent = 0
            while totalsent < len(msg):
                sent = self.socket.send(msg[totalsent:])
                if sent == 0:
                    self.running = False
                    print "LOG: Socket connection broken during write"
                    return
                totalsent = totalsent + sent

    def recv(self, len):
        try:
            return self.socket.recv(len)
        except:
            # TODO: Make this log something, and make it a more specific
            # exception trap - AFAIK, it only happens during shutdown.
            self.close()
            return ''
        
    def write(self, msg):
        try:
            self.send(msg)
            self.send("\0")
        except Exception as e:
            # I suspect that error handling elsewhere wil catch this.
            print "OMG: %s" % e
            print "IF WE DO NOT SHUT DOWN THERE IS A BUG HERE!"
            self.close()

    def close(self):
        if self.running:
            try:
                self.socket.shutdown(socket.SHUT_RD)
                #print "CLIENTSOCKET CLOSED FOR READ: %s" % clientsocket
            except: pass # Nothing much to do if we get an error here.
            self.running = False

    def shutdown(self):
        self.close()
        killSocket(self.socket)
        self.deaded.deaded = True
        killSocket(self.serversocket)

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
            chunk = self.recv(1024)
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
        while self.running:
            chunk = self.get_msg()
            if chunk:
                buffer = buffer + chunk
                lines = buffer.split('\n')
                if len(lines) > 0:
                    for line in lines[:-1]:
                        self.controller.put((self, line))
                buffer = lines[-1]
            else:
                break
        if buffer != "":
            self.controller.put((self, "%s\n" % buffer))
        self.socket.close()
        
class OnTimeout(ThreadPlus):
    def __init__(self, timeout, func, *args):
        super(OnTimeout, self).__init__()
        self.timeout = timeout
        self.func = func
        self.args = args
        self.start()

    def stop(self):
        super(OnTimeout, self).stop()
        
    def run(self):
        if self.sleep(self.timeout):
            print "TIMED-OUT"
            # We reached our timeout
            self.func(*self.args)

def killSocket(socket, msg=None):
    if msg:
        sys.stderr.write("%s\n" % msg)
    try:
        socket.shutdown(socket.SHUT_RDWR)
    except Exception: pass
    try:
        socket.close()
    except Exception: pass

class Deaded:
    """Dumb class to record whether something has been killed."""
    def __init__(self):
        self.deaded = False

    def __call__(self):
        return self.deaded

def cleanupConnections(connections):
    items = len(connections)
    this = 0
    while this < items:
        if connections[this].running:
            this += 1
        else:
            del(connections[this])
            items -= 1
    
if __name__ == '__main__':
    import socket
    import optparse
    import traceback

    parser = optparse.OptionParser()
    parser.add_option("-p", "--port", type=int, dest="port", default=8888,
                      help="Run volumed server using specified port")
    parser.add_option("-e", "--emulate",  dest="emulate",
                      action="store_true", help="Emulate the hw interface")

    (options, args) = parser.parse_args()
    dirname = os.path.dirname(sys.argv[0])
    db = DB("%s/db/player.db" % dirname)

    interface = HWInterface(db, options.emulate)
    monitor = Monitor(interface, db)
    controller = VolumeController(monitor, interface, db)
    result = 0
    
    serversocket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    serversocket_deaded = Deaded()
    #print "SERVERSOCKET: %s" % serversocket
    try:
        connections = []
        to = OnTimeout(5, killSocket, serversocket,
                       "Timed-out while trying to bind server socket.")
        serversocket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        serversocket.bind(('', options.port))
        to.stop()
        serversocket.listen(5)
        serversocket.settimeout(0.2)
        
        while True:
            # accept connections from outside
            try:
                (clientsocket, address) = serversocket.accept()
                #print "CLIENTSOCKET: %s" % clientsocket
            except socket.timeout:
                continue
            except Exception:
                if serversocket_deaded():
                    # We killed the serversocket, so this exception is
                    # expected (whatever it is).
                    break
                raise
            connections.append(
                Interlocutor(clientsocket, controller, serversocket,
                             serversocket_deaded))
            cleanupConnections(connections)

    except Exception as e:
        traceback.print_exc()
        result = 2
    finally:
        to.stop()
        controller.stop()
        for i in connections:
            i.close()
        controller.join()
        killSocket(serversocket)
        sys.exit(result)
