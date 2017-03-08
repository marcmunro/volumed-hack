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
# Tim, if you would like, I will assign the copyright to you.
#

# TODO:
# Read volume from amixer/mpc
# Set volume using amixer/mpc
# Test with named pipes
# Update vol.php and vol.sh to use this
# Move handling of logarithmic volume control into here from javascript
# Modify javascript to be more responsive

import sys
import os
import threading
import time
import Queue
import re
import sqlite3

def usage(msg):
    sys.stderr.write("ERROR: %s\n\n" % msg)
    sys.stderr.write("TODO: Write usage message.\n" % msg)
    sys.exit(2)

def open_stream(name, errmsg, write=False):
    """Simple utility function to open a stream so that we can use '-'
    to mean stdout/stdin."""
    try:
        if name == "-":
            if write:
                stream = sys.stdout
            else:
                stream = sys.stdin
        else:
            stream = open(name, "a" if write else "r")
        return stream
    except IOError as e:
        usage("%s\n\n%s\n" % (errmsg, e))


class Conduit:
    """Provides a mechanism to communicate to a volumed client.
    Initially this will simply provide access to the in and out streams."""
    def __init__(self, instream, outstream):
        self.instream = instream
        self.outstream = outstream

    def write(self, str):
        self.outstream.write(str)

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
        self.last_dequeued = None
        self.volume_re = re.compile("^ *Vol *([+-])?([0-9]*) *$",
                                    re.IGNORECASE)
        self.mute_re = re.compile("^ *(Un)?Mute *$", re.IGNORECASE)
        self.quit_re = re.compile("^ *q(uit)? *$", re.IGNORECASE)
        self.start()

    def put (self, payload):
        self.queue.put(payload)
        
    def parse_item(self, item):
        match = self.volume_re.match(item)
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
                    cmd = 'set'
                else:
                    cmd = 'get'
        else:
            match = self.mute_re.match(item)
            if match:
                val = 0
                if match.group(1):
                    cmd = 'unmute'
                else:
                    cmd = 'mute'
            else:
                if self.quit_re.match(item):
                    cmd = 'quit'
                    
        print "CMD: %s, VAL: %s (item: \"%s\")" % (cmd, val, item)            
        return (cmd, val)

    def set_mute(self):
        self.db.mute = '1'
        self.hw_interface.set_volume(0)
    
    def set_unmute(self):
        level = int(self.db.level)
        self.hw_interface.set_volume(level)
        self.monitor.set_volume(level)
        self.db.mute = '0'
    
    def set_volume(self, vol):
        warn_level = int(self.db.warning_level)
        if vol < 0:
            vol = 0
        elif vol > warn_level:
            vol = warn_level
        if self.db.mute == '0':
            self.hw_interface.set_volume(vol)
            self.monitor.set_volume(vol)
        
    def process_items(self, conduit, items):
        """This combines a stream of possibly related commands in items
        into grouped get and/or set operations.  This means that a
        fast stream of vol +1 commands will likely be concatebated
        into a single vol +N command.  The point of this is to improve
        responsiveness to things such as remote controls.
        TODO:  be smarter about merging commands from multiple
        conduits."""

        self.monitor.reset_wait()   # No point in fetching current volume any
                                    # time soon.
        vol = self.monitor.volume()
        set = False
        get = False
        mute = False
        unmute = False
        quit = False
        for item in items:
            cmd, val = self.parse_item(item)
            if cmd:
                if cmd == 'get':
                    get = True
                elif cmd == 'set':
                    set = True
                    vol = val
                elif cmd == 'delta':
                    set = True
                    vol += val
                elif cmd == 'mute':
                    mute = True 
                    unmute = False
                elif cmd == 'unmute':
                    unmute = True 
                    mute = False
                elif cmd == 'quit':
                    quit = True
                    break
        if mute:
            self.set_mute()
            conduit.write("Muted\n")
        if set:
            self.set_volume(vol)
            conduit.write("Vol: %d\n" % self.monitor.last_volume)
        if unmute:
            self.set_unmute()
            conduit.write("Unmuted\n")
        if get:
            conduit.write("Vol: %d\n" % self.monitor.last_volume)
        if quit:
            self.stop()
    
    def get_items(self):
        """Compile a list of related requests (all coming from the same
        conduit) and return the conduit and the list.  The conduit will
        provide the means to provide any needed feedback back to the
        requester."""
        if self.last_dequeued:
            conduit, item = self.last_dequeued
            self.last_dequeued = None
        else:
            conduit, item = self.queue.get()
        items = [item]
        while not self.queue.empty():
            this_conduit, item = self.queue.get(False)
            if this_conduit != conduit:
                self.last_dequeued = (conduit, item)
                break
            items.append(item)
        return conduit, items

    def run(self):
        # This is where we asynchronously parse and process commands
        # read from our input stream.  Note that although this is
        # asynchronous with regard to the input stream it is synchronous
        # with regard to our command stream.  IE, we only process the
        # queue once any previous command has been completely processed.
        while self.running:
            conduit, items = self.get_items()
            self.process_items(conduit, items)
            
class Monitor(ThreadPlus):
    RESOLUTION = 2.0
    
    """Class to provide periodic monitoring of volume control.  
    This allows us to discover volume changes caused by events outside of
    our control."""

    def __init__(self, hw_interface, db):
        super(Monitor, self).__init__()
        self.hw_interface = hw_interface
        self.db = db
        self.last_volume = self.fetch_volume()
        self.volume_lock = threading.Lock()
        self.wake_time = 0
        self.start()

    def fetch_volume(self):
        return self.hw_interface.volume()
        
    def volume(self):
        return self.last_volume
        
    def set_volume(self, volume):
        """Update the volume if it has changed.  Note that this may
        safely  be called asynchronously."""
        if (volume == 0) and (self.db.mute == '0'):
            return # Do not update our record of the desired volume

        with self.volume_lock:
            if (self.last_volume != volume):
                self.last_volume = volume
                update_db = True
            else:
                update_db = False
        if update_db:
            self.db.level = volume
        
    def reset_wait(self):
        """Reset our sleep timeout time."""
        self.set_sleep_target(time.time() + Monitor.RESOLUTION)

    def run(self):
        while self.running:
            if self.sleep(Monitor.RESOLUTION):
                vol = self.fetch_volume()
                self.set_volume(vol)
                
class HWInterface:
    """Provide an interface to the volume control hardware."""

    def __init__(self, db):
        self.db = db
        self.cardnum = self.get_cardnum()

    def get_cardnum(self):
        """Based on the original vol.sh, though I am not entirely
        convinced.  My use case for the music box includes having a usb
        audio capture device.  I fear that such an extra card may make
        this approach fail."""
        try:
            open("/proc/asound/card1/id")
            return 1
        except IOError:
            return 2
        
    def volume(self):
        with open("./voltmp") as f:
            print "TODO: READ HW VOLUME"
            return int(f.read().strip())

    def set_volume(self, volume):
        if self.db.mpd_mixer == 'hardware':
            if self.db.volcurve == 'Yes':
                cmd = ("amixer -c %d sset %s -M%d%%" %
                       (self.cardnum, self.db.alsa_mixer, volume))
            else:
                cmd = ("amixer -c %d sset %s %d%%" %
                       (self.cardnum, self.db.alsa_mixer, volume))
        else:
            cmd = "mpc volume %d" % volume
        print "CMD: %s" % cmd
        
        time.sleep(2)
                
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
        self.makeField(field)
        print "DB: set %s to %s" % (field, value)
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
    
        
if len(sys.argv) < 3:
    usage("Insufficient arguments")
elif len(sys.argv) > 3:
    usage("Unexpected extra arguments: %s" % (sys.argv[3:]))

dirname = os.path.dirname(sys.argv[0])
db = DB("%s/db/player.db" % dirname)

interface = HWInterface(db)
monitor = Monitor(interface, db)
instream = open_stream(sys.argv[1],
                       "Cannot open \"%s\" for input" % sys.argv[1])
outstream = open_stream(sys.argv[2],
                        "Cannot open \"%s\" for writing" % sys.argv[2],
                        True)

conduit = Conduit(instream, outstream)

v = VolumeController(monitor, interface, db)

while v.running:
    res = instream.readline()
    if res == "":
        v.stop()
    else:
        v.put((conduit, res.strip()))
v.join()
monitor.stop()
db.stop()
