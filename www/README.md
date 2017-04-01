Volume daemon hack for moode audio
==================================

This hack adds a volume daemon webservice for moodeaudio and modifies
moode to make use of it.

The volume daemon allows much more responsive control of volume from the
moode web interface.  This transforms drag and drop control of volume
from being barely usable on a pi-based moode player into a fast,
responsive and pleasant experience.  It also provides timely feedback
when volume or mute is changed by other actors.

User Visible Changes
--------------------

The volume knob display has been split into into two parts.  The
selector knob, itself which now appears as a notch, and the rotary
slider volume display.  When changing the volume through the UI, the
notch moves in real time with no lag whatsoever.  The slider catches up
as the selected changes are made on the player.

If volume is changed by some other cause (eg another user on a separate
UI), the rotary slider quickly shows the change, and the notch will
catch-up a little later.  Whenever the notch and the rotary slider are
out of step, the numeric volume display will be shown in green.  When
they are in step, the numeric display changes to white (the same colour
as the playback time indicator).

Lost Functionality
------------------

Volume curve correction is not currently implemented.  See Future
Directions below.

Architectural Changes
---------------------

Volumed is now responsible for directly controlling `mpd` and/or `amixer`.
It accepts volume changing commands and asynchronously applies them to
the hardware.  When the volume, or mute status, changes it sends
notifications back through the websocket.  It is also responsible
(currently - see Future Directions below) for updating the `sqlite3`
database to record such changes.

Volumed also implements muting through the amixer mute mechanism if
hardware volume control is being used.

The javascript client interface has been changed to make use of
volumed.  This means it no longer has to deal with database updates or
directly manipulate amixer or mpd.  If it cannot maintain contact with
the volume daemon it falls back to its old behaviour (though with the
notched volume control).

Dependencies and Installation
-----------------------------

`volumed.py` and `volumec.py` are implemented in python.  The following
extra packages are required:

- `python-gevent`
  Installed using `apt-get install python-gevent`

- `ws4py'
  Not available as a debian package so installed from git.  The version
  I used was from commit `641d3c6d073d9c7ebd738c68359417c7b088d6a5`.

  Copy the ws4py directory into:
  `/usr/local/lib/python2.7/site-packages`

`volumed.py` and `volumec.py` are currently located in `/var/www`.  This
will be corrected when (if) volumed is properly packaged.

The service files for systemd are `volumed.service` and
`volumec.service`.  Copy these files to /lib/systemd/system/ and then
run:

>    `# systemctl enable volumed.service`

>    `# systemctl enable volumec.service`


The volumed protocol
--------------------

Commands are sent to volumed and the current status is returned.
Commands may internally be aggregated (eg `vol +1`, `vol +1`, `vol +1`,
may be aggregated into a single `vol +3` which is sent to the hardware).
In such a case, only the last command will result in a response.  Also
responses will be sent whenever volumed detects that the volume or mute
status has changed.

This means that although sending a command will usually result in a
single response, this is not guaranteed.  Volumed is intended for
asynchronous use.

The commands accepted by volumed are:

- `vol`
  A volume query, which will illicit a response, though multiple queries
  may be aggregated resulting in only a single response.

- `vol +n`
  Increase volume by n percentage units.  A response is illicited.
  Volume is limited to the max percentage set in the database.

- `vol -n`
  Increase volume by n units.  A response is illicited.  Volume cannot
  decrease below zero.

- `vol n`
  Set volume to n%.  A response is illicited.

All reponses are in the form:

>    `Vol: 99, Mute: off`

The volume value is a percentage from 0 to 100.  The values of mute are
"off" and "on".

Volumec
-------

In addition to the volumed daemon, there is another executable,
`volumec.py`  This is a volumed client, allowing a command-line user
interface to volumed.  It can be used as a one-shot command,
interactively through stdin, through a named-pipe, or as a daemon lirc
client.  This allows infra-red remote controls to be easily set up with
good responsiveness.

Single-shot (command) usage:

>    `$ volumec.py -c "command"`

(where command will be `vol`, `vol +N`, etc)

Interactive usage:

>    `$ volumec.py`

commands may be typed interactively, responses will be shown when they
are received.

Through a named-pipe (fifo)

>    `$ mkfifo my-named-pipe`
>    `$ volumec.py -f my-named-pipe -o`

And then to send a command, simply echo it to the named pipe:

>    `$ echo "vol +3" my-named-pipe`

As an lirc client (called volumec):

>    `$ volumec.py -d -q`

In this mode button presses from an infra-red control will be passed to
volumec, which will in turn pass them on to volumed.  This is much
faster and more responsive than using irexec to execute shell commands.

Example lines from an lircrc file follow:

    begin
        button = KEY_VOLUMEDOWN
        repeat = 1
        prog = volumec
        config = vol -1
    end
    begin
        button = KEY_VOLUMEUP
        repeat = 1
        prog = volumec
        config = vol +1
    end
    begin
        button = KEY_MUTE
        prog = volumec
        config = mute 
        config = unmute
    end


Future Directions (and critique)
--------------------------------

The volumed daemon should not access the moode database.  This limits
its usefulness to moode audio, though the general mechanism should work
well for volumio, runeaudio and others.

The proper way to do this would be to use a configuration file to
configure max_pct, hardware volume control, etc, and to create a
websocket client to manage database access.  Different clients could
then be built for moode, volumio, runeaudio, etc.

It might also be appropriate to allow configuration to be performed by
sending special configuration commands.

Volume curve correction should be added.  Note that this needs to be
done for both reading and writing the volume.

Ideally both volumed and volumec would be re-implemented using a
compiled language, as the current python implementation is far less
efficient than is ideal.

Volumed should become a debian package.

