#!/usr/bin/env python
# -*- coding: utf-8 -*-

# XPEAK: a user-friendly command-line front-end to espeak
# Copyright (C) 2018 Esteban Flamini <http://estebanflamini.com>

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

from __future__ import print_function

__version__ = '1.0.1'

# Import needed Python packages. I like to import them whole: when you find an unqualified
# function invocation below, either it's a built-in or it's defined somewhere in this file.

import Queue
import readline
import sys
import time
import threading
import termios
import tty
import signal
import gettext
import subprocess
import os
import re
import signal
import argparse
import locale
import select
import codecs
import difflib
import textwrap
import shlex
import traceback

# This is a multi-threaded, event-driven program.

# Structure of this file (I tried to arrange it mostly in a top-down fashion):

# - Global vars
# - Main routine
# - Termination utility
# - Argument parsing
# - Configuration file reading
# - Input file reading
# - The event loop
# - Semantics (bindings) for commands
# - Key bindings (key -> command)
# - User input daemon
# - File monitoring daemon
# - Definition of the Player (a singleton orchestrating the sending of text to espeak via the caller)
# - Implementation of substitutions
# - Code for searching
# - Code for moving to a specific line
# - A printing utility ('say')
# - Global utilities
# - Utilities related to terminal settings
# - Program entry point


# Localisation should be easy
gettext.install('xpeak',os.environ['TEXTDOMAINDIR'])


######################################################################################
# GLOBAL VARS

ESPEAK    = 'espeak'
CONF_FILE = sys.path[0] + '/xpeak.cfg'
XPLIT     = sys.path[0] + '/xplit.py'
XTXT      = sys.path[0] + '/xtxt.py'

args = None  # 'Args' (command line options) will be populated at start time

locales = {} # Same as above

input_file      = None  # File to be read
monitored_file  = None  # File to be monitored for changes, usually the same as input_file
input_command   = None  # Command to get the text content of the input_file
input_path      = None  # Path where the input_file is
splitting_rules = None  # Splitting-rules file

lang, encoding = locale.getdefaultlocale()
if encoding.lower() in ['utf-8','utf8']:
  encoding = 'utf-8-sig'

subst_files    = [] # A list of substitution files
subst_match    = [] # A list of 'match' rules read from substitution files
subst_replace  = [] # A corresponding list of substitutions for the 'match' rules
subst_location = [] # For each rule, the name of the file it came from

player = None # A singleton in charge of orchestrating the reading of sentences with espeak.
              # Will be created at start time, after getting args.

event_queue = Queue.PriorityQueue() # Events to be dispatched to the player or other routines

# The event queue is populated with events from three daemon threads:

# 1. file monitoring daemon (tracks modifications to any of the underlying files)
# 2. user input daemon (takes commands from standard input)
# 3. worker daemon (a part of the player) - it can put error events and the QUIT event in the queue

# Events in the event queue are read and dispatched by the event loop, which runs in another thread.

# Event types and priorities

P_QUIT   = 1 # Event: the user sent the 'quit' or 'QUIT' command. Maximum priority
P_ERR    = 2 # Event: an error has occurred
P_CMD    = 3 # Event: the user sent a command other than 'quit' or 'QUIT'
P_MOD    = 4 # Event: some underlying file has changed on disk


######################################################################################
# MAIN ROUTINE

def main():
  global player

  # Ensure all output is properly encoded
  if not sys.stdout.isatty():
    sys.stdout = codecs.getwriter(encoding)(sys.stdout)

  get_config()

  get_args()

  print(file=sys.stderr) # Separate first line of output

  # Allow only one instance at a time, unless called with --force-execution
  if not args.force_execution:
    pids = subprocess.check_output('pgrep -a python' ,shell=True).split("\n")
    pids = filter(None,pids)
    pids = filter(lambda x : __file__ in x,pids)
    pids = filter(lambda x : not str(os.getpid()) in x,pids)
    if pids:
      terminate(_('An instance is already running.'))

  text = load_text()
  if text:
    player = Player(text)
  else:
    terminate(_('Empty file/source.'))

  # Set the terminal to 'raw' mode
  init_tty()

  # Set SIGUSR1 and SIGTERM signals to gracefully terminate the program.
  signal.signal(signal.SIGUSR1,lambda signum,frame: terminate())
  signal.signal(signal.SIGTERM,lambda signum,frame: terminate())

  # Set SIGUSR2 to check monitored files for changes and restart if needed.
  signal.signal(signal.SIGUSR2,lambda signum,frame: file_daemon_check_files(say_it=True))

  # Listen to events in another thread, and use the documented feature signal.pause to wait in the main
  # thread for signals to be sent.
  start_daemon(event_loop)
  while True:
    # TODO portability issue here
    signal.pause()


######################################################################################
# PROGRAM TERMINATION UTILITY

main_thread = threading.currentThread()

termination_cause = None

# Exit xpeak (with an optional error message). If called from the main thread, terminate using sys.exit.
# If called from another thread, generate a SIGUSR1 signal, which will then reinitiate the call from
# the main thread.
def terminate(msg=None):
  global termination_cause

  if threading.currentThread() == main_thread:
    # Do the required cleanup first
    if player is not None:
      player.stop(say_it=False)
    restore_tty()

    msg = msg or termination_cause
    if msg is not None:
      say(msg)
      sys.exit(1)
    else:
      sys.exit(0)
  else:
    termination_cause=msg
    os.kill(os.getpid(),signal.SIGUSR1)


######################################################################################
# PARSE COMMAND LINE ARGUMENTS, SET GLOBAL VARS

def get_args():
  global args
  global input_file
  global monitored_file
  global input_command
  global input_path
  global splitting_rules

  parser = argparse.ArgumentParser(prog='xpeak.py')
  parser.add_argument('--version',action='version',version='%(prog)s ' + __version__)
  parser.add_argument('--lang',default=lang[0:2])
  parser.add_argument('-v','--voice',default=None)
  parser.add_argument('-p','--pause-before',nargs='?',const=3,default=0,type=int)
  parser.add_argument('-m','--monitoring-interval',default=2,type=int)
  parser.add_argument('-M','--monitored-file')
  parser.add_argument('-a','--always-reload-after-change',action='store_true',default=False)
  parser.add_argument('-R','--force-restart-after-change',action='store_true',default=False)
  parser.add_argument('-T','--do-not-track',action='store_true',default=False)
  parser.add_argument('-u','--show-subst',action='store_true',default=False)
  parser.add_argument('-l','--show-line-numbers',action='store_true',default=False)
  parser.add_argument('-d','--do-not-close-after-EOF',action='store_true',default=False)
  parser.add_argument('-X','--do-not-split',action='store_true',default=False)
  parser.add_argument('-f','--force-execution',action='store_true',default=False)
  parser.add_argument('-s','--speed',default=180,type=int)
  parser.add_argument('-b','--backward-skipping-stops-playing',action='store_true',default=False)
  parser.add_argument('-c','--long-commands',action='store_true',default=False)
  parser.add_argument('-z','--remove-newline',action='store_true',default=False)
  parser.add_argument('-L','--stop-after-each-line',action='store_true',default=False)
  parser.add_argument('-Q','--quit-without-asking',action='store_true',default=False)
  parser.add_argument('-S','--subst-file')
  parser.add_argument('--splitting-rules')
  parser.add_argument('-r','--raw',action='store_true',default=False)
  parser.add_argument('-q','--quiet', action='count')
  parser.add_argument('--opt',default='',help='Use the --opt=\'-opt1 -opt2 ...\' syntax.')
  group = parser.add_mutually_exclusive_group(required=True)
  group.add_argument('--do')
  group.add_argument('file',nargs='?')
  args = parser.parse_args()

  if args.lang not in locales:
    terminate(_('Invalid language: %s. Valid options are %s.') % (args.lang,', '.join(locales.keys())))

  if args.do is not None:
    input_command = args.do
    input_path = '.'
  elif args.file is not None:
    if not os.path.exists(args.file):
      terminate(_('File %s does not exist.') % args.file)
    input_file = unicode(args.file,encoding)
    input_path = os.path.dirname(args.file) or '.'
    input_command = "%s --lang %s '%s'" % (XTXT, args.lang, input_file)
    monitored_file = args.file
  if args.file is None and args.monitored_file is not None:
    if os.path.exists(args.monitored_file):
      monitored_file = args.monitored_file
    else:
      print(file=sys.stderr)
      terminate(_('The file %s does not exist.') % args.monitored_file)
  if args.remove_newline:
    input_command += ' | sed -z \'s/\\n/ /g\''
  if not args.do_not_split:
    if args.splitting_rules is not None:
      if os.path.isfile(args.splitting_rules):
        splitting_rules = args.splitting_rules
      else:
        print(file=sys.stderr)
        terminate(_('The file %s does not exist.') % args.splitting_rules)
    else:
      for p in [os.path.dirname(args.file or '') or '.' , sys.path[0] ]:
        fn1 = p + '/xplit.rules'
        for fn2 in ['.' + args.lang,'']:
          if os.path.isfile(fn1 + fn2):
            splitting_rules = fn1 + fn2
            break
    sr = ' --splitting-rules %s ' % splitting_rules if splitting_rules is not None else ''
    input_command += ' | %s %s -s' % (XPLIT,sr)

  subst_files.append(input_path + '/xpeak.subst.' + args.lang)
  subst_files.append(input_path + '/xpeak.subst')
  if args.subst_file is not None:
    subst_files.append(args.subst_file)
  subst_files.append(sys.path[0] + '/xpeak.subst.' + args.lang)
  subst_files.append(sys.path[0] + '/xpeak.subst')
  subst_files.append(input_path + '/xpeak.subst.post.' + args.lang)
  subst_files.append(input_path + '/xpeak.subst.post')
  if not args.raw:
    load_subst()

  init_monitored_files()


######################################################################################
# READ CONFIGURATION FILES

def get_config():
  try:
    with open(CONF_FILE) as f:
      conf = f.read()
  except IOError:
    terminate(_('Unable to open file: %s.') % CONF_FILE)
  for m in re.finditer(r'(?ms)^\s*locale:\s*(\S+)\s*$(.+?)\n{2,}',conf):
    m2 = re.search(r'(?m)^\s*voice:\s*(\S+)\s*$',m.group())
    voice = m2 and m2.group(1) or None
    m2 = re.search(r'(?m)^\s*sam:\s*(.+)\s*$',m.group())
    sam = m2 and m2.group(1).strip() or None
    if voice is not None:
      locales[m.group(1)] = {'voice': voice, 'sam': sam}


######################################################################################
# READ INPUT FILE

# Load and return the input file as a list, or None in case of error
def load_text():
  if input_file is not None:
    if not os.path.exists(input_file):
      say(_('Cannot find input file: %s.') % input_file)
      return None
    say(_('Reading file: %s ...') % input_file)
  else:
    say(_('Reading output from: %s ...') % args.do)
  try:
    tmp = unicode(subprocess.check_output(['sh','-c',input_command]),encoding)
    if input_file is not None:
      say(_('Read: %s') % input_file)
  except:
    say(_('An error has occurred while reading the file: %s.') % input_file,muteable=False)
    return None
  if tmp:
    tmp = re.sub('\n{2,}','\n',tmp)
    tmp = re.findall('^.*?\S.*?$', tmp, re.M)
  return tmp


# Compare two versions of text and return a list of changed lines' numbers.
# 'new' and 'old' are lists of sentences.
def compare_text(new,old):

  s = difflib.SequenceMatcher(None,new,old)
  tmp = filter(lambda x : x[0] != 'equal',s.get_opcodes())
  modified_lines = []
  for x in tmp:
    if x[0] == 'insert':
      modified_lines.extend(range(x[1],x[1] + 1))
    else:
      modified_lines.extend(range(x[1],x[2]))

  return modified_lines


######################################################################################
# EVENT LOOP

# The event loop runs in its own thread. It takes care of starting other daemon threads which populate
# the event queue.

def event_loop():
  pause(args.pause_before)
  if not args.do_not_track:
    start_daemon(file_daemon)
  start_daemon(wait_for_cmd)
  player.go(0)
  player.start()
  while True:
    (priority, event) = event_queue.get()
    if priority == P_QUIT:
      # The user has issued a quit/QUIT a command (the command itself is reported in 'event'):
      player.stop(False)
      if confirmation_to_quit(event):
        terminate()
      else:
        say_stopped()
        start_daemon(wait_for_cmd)
    elif priority == P_CMD and event in cmd_bindings: # cmd_bindings defined just below
      # The user has issued a command other than quit/QUIT (the command itself is reported in 'event'):
      action = cmd_bindings[event]
      action()
      # Restart the user input daemon
      start_daemon(wait_for_cmd)
    elif priority == P_MOD:
      # An underlying file has changed on disk. Let the player know:
      player.file_modified(event)
    elif priority == P_ERR:
      # An irrecoverable error occurred, xpeak must stop
      terminate(event)


def confirmation_to_quit(cmd):
  if cmd == 'quit' and not args.quit_without_asking and not args.long_commands:
    say(_('Do you really want to quit (Y/n)?'),muteable=False)
    while True:
      ch = getch().lower()
      if ch in [_('y') , chr(13) , chr(10)]:
        return True
      elif ch == _('n'):
        return False
  else:
    return True


######################################################################################
# BINDINGS FOR "LONG COMMANDS"

cmd_bindings = {
  'toggle'      : lambda : player.toggle(),
  'stop'        : lambda : player.stop(),
  'first'       : lambda : player.first(False),
  'last'        : lambda : player.last(False),
  'firststop'   : lambda : player.first(True),
  'laststop'    : lambda : player.last(True),
  'next'        : lambda : player.forward(),
  'back'        : lambda : player.back(args.backward_skipping_stops_playing),
  'backalt'     : lambda : player.back(not args.backward_skipping_stops_playing),
  'stopafter'   : lambda : player.stop_after_current_track(),
  'oneline'     : lambda : player.stop_after_each_line(),
  'again'       : lambda : player.again(),
  'togglesubst' : lambda : player.toggle_subst(),
  'showsubst'   : lambda : player.toggle_show_subst(),
  'printsubst'  : lambda : print_subst_hist(),
  'lineno'      : lambda : player.toggle_line_numbers(),
  'showline'    : lambda : player.show_line(mandatory=True),
  'findplainci' : lambda : find(regex=False,cs=False),
  'findplaincs' : lambda : find(regex=False,cs=True),
  'findregexci' : lambda : find(regex=True,cs=False),
  'findregexcs' : lambda : find(regex=True,cs=True),
  'findnext'    : lambda : find_next(),
  'findlast'    : lambda : find_last(),
  'goline'      : lambda : go_line(),
  'faster'      : lambda : player.change_speed(10),
  'slower'      : lambda : player.change_speed(-10),
  'openfile'    : lambda : edit_file(input_file),
  'openxrules'  : lambda : edit_file(splitting_rules),
  'opensubst'   : lambda : open_subst_file(),
  'openshell'   : lambda : open_shell(),
  'reload'      : lambda : player.reload_file(),
  'checkfiles'  : lambda : file_daemon_check_files(say_it=True),
  'quit'        : None, # No need to bind, semantics is implemented in event_loop
  'QUIT'        : None  # No need to bind, semantics is implemented in event_loop
}


######################################################################################
# KEY BINDINGS

# TODO: migrate this to an external config file, to be loaded at entry point; keep this hardcoded
# key bindings as default, in case xpeak is unable to read that config file

key_bindings = {
  'q': 'quit',
  'Q': 'QUIT',
  ' ': 'toggle',
  'x': 'stop',
  'V': 'first',
  'M': 'last',
  'v': 'firststop',
  'm': 'laststop',
  'n': 'next',
  'b': 'back',
  'B': 'backalt',
  '.': 'stopafter',
  ',': 'oneline',
  'a': 'again',
  'S': 'togglesubst',
  'D': 'showsubst',
  'u': 'printsubst',
  'l': 'lineno',
  'w': 'showline',
  'f': 'findplainci',
  'F': 'findplaincs',
  'r': 'findregexci',
  'R': 'findregexcs',
  't': 'findnext',
  'e': 'findlast',
  'g': 'goline',
  '+': 'faster',
  '-': 'slower',
  'o': 'openfile',
  's': 'opensubst',
  ':': 'openxrules',
  'c': 'openshell',
  'L': 'reload',
  'C': 'checkfiles'
}


######################################################################################
# USER INPUT DAEMON
# It reads single-char keystrokes and translates them into long commands, unless xpeak was called
# with option --long-commands, in which case long commands are read as whole lines.
# Then, the command is added to the event queue.

# This function runs in a separate daemon thread. It terminates after queing a command into the
# event queue, to ensure the event processing thread has control of standard input if needed
# (e.g., 'search' and 'go' commands need further input from the user). The user input thread
# will be restarted by the event loop after processing a command.

# TODO: implement non-printing keys, such as arrows, PgDn, etc.

def wait_for_cmd():
  while True:
    if args.long_commands:
      cmd = sys.stdin.readline().strip().lower()
    else:
      ch = getch()
      if ch in key_bindings:
        cmd = key_bindings[ch]
      elif ch.isalpha() and ch.isupper() and ch.lower() in key_bindings:
        say(_('Unrecognized command (CapsLock ON?)'),muteable=False)
        continue
      else:
        cmd = None
    if cmd in cmd_bindings:
      event_queue.put((P_QUIT if cmd in ['quit','QUIT'] else P_CMD,cmd))
      return


######################################################################################
# FILE MONITORING DAEMON
# This daemon checks to see whether any of the underlying files (target file, substitution-rule files
# and/or splitting-rule file) was modified, and generates an event if so.
# The monitoring function (file_daemon) runs in a separate daemon thread.

targets = {}

def init_monitored_files():
  if monitored_file is not None:
    targets[monitored_file] = os.path.getmtime(monitored_file)
  if splitting_rules is not None:
    targets[splitting_rules] = os.path.getmtime(splitting_rules)
  for f in subst_files:
    if os.path.isfile(f):
      try:
        targets[f] = os.path.getmtime(f)
      except IOError as e:
        event_queue.put((P_ERR,error_msg % e))
        return
    else:
      targets[f] = None

def file_daemon():
  while True:
    time.sleep(args.monitoring_interval)
    file_daemon_check_files()

error_msg = _('Error while trying to get modification time: %s.')

def file_daemon_check_files(say_it=False):
  if say_it:
    say(_('Checking files for changes.'))
  try:
    changes_detected = False
    for f in targets:
      if os.path.isfile(f) or os.path.isdir(f):
        mtime = os.path.getmtime(f)
        if mtime != targets[f]:
          targets[f] = mtime
          say(_('Modified: %s.') % f)
          event_queue.put((P_MOD, f))
          changes_detected = True
    if not changes_detected and say_it:
      say(_('No changes detected.'))
  except IOError as e:
    event_queue.put((P_ERR,error_msg % e))
  except:
    pass # Most likely to occur at interpreter shutdown


######################################################################################
# THE PLAYER: a singleton which orchestrates the reading of the input text through espeak

class Player:

  def __init__(self,text):

    self.text               = text
    self.old_text           = text

    self.worker_thread      = None
    self.espeak             = None
    self.lock               = threading.RLock()
    self.playing            = False
    self.paused             = False

    self.track              = -1              # Current line being read
    self.apply_subst        = not args.raw    # Should we apply substitutions?
    self.line               = None            # Text being read with substitutions possibly applied
    self.load               = False           # Should the text be reloaded before restarting?

    self.speed              = args.speed
    self.show_subst         = args.show_subst  # Should we show substituted text on screen?
    self.show_line_numbers  = args.show_line_numbers

    self._stop_after_each_line     = args.stop_after_each_line
    self._stop_after_current_track = False

  # A method of class Player.
  def get_text(self):
    with self.lock:
      return self.text

  # A method of class Player.
  def current_track(self):
    with self.lock:
      return self.track

  # A method of class Player: update line pointer
  def go(self,track):
    with self.lock:
      self.track = track
      self.update_line()
      self.show_line()
      if not self.line.strip():
        self.line = ' ';

  # A method of class Player: update the text to be sent to espeak, with substitutions if needed
  def update_line(self):
    with self.lock:
      self.line = apply_subst(self.text[self.track]) if self.apply_subst else self.text[self.track]

  # A method of class Player: print the current line as required
  def show_line(self,mandatory=False):
    with self.lock:
      t = self.line if self.show_subst else self.text[self.track]
      t = ('<%s> ' % str(self.track + 1) if self.show_line_numbers else '') + t
      say(t,track = self.track,prompt = False,mandatory=mandatory)

  # A method of class Player.
  def start(self):
    with self.lock:
      if not self.playing:
        self.playing = True
        self.paused = False
        self.worker_thread = start_daemon(lambda : self.worker())

  # A method of class Player.
  # Runs in its own thread.
  def worker(self):
    while True:
      with self.lock:
        self.espeak = self.call_espeak()
        if self.espeak is None:
          self.playing = False
          self.paused = False
          return
        if self.paused:
          self.pause_espeak()
      self.espeak.wait()
      with self.lock:
        if not self.playing:
          return
        elif self.espeak.returncode:
          say(_('espeak terminated with return code: %s.') % self.espeak.returncode)
          say(_('The reading is stopped, you can try to restart it.'))
          self.playing = False
          self.paused = False
          return
        elif not self.advance():
          self.playing = False
          self.paused = False
          return

  # A method of class Player: start a new instance of espeak.
  # Always called from the worker thread, with lock acquired.
  def call_espeak(self):
    line = self.line
    line = re.sub('^\s*-',r'\-',line) # to avoid an initial hyphen to be taken as an option
    voice = args.voice or locales[args.lang]['voice']
    if args.opt:
      d = [ESPEAK,'-s',str(self.speed),'-v',voice] + shlex.split(args.opt) + [line]
    else:
      d = [ESPEAK,'-s',str(self.speed),'-v',voice,line]
    try:
      self.terminated_by_stop = False
      return subprocess.Popen(d)
    except Exception as e:
      traceback.print_stack()
      say(_('Cannot run espeak: %s. The reading is stopped, you can try to restart it.') % e,
          muteable=False)
      return None

  # A method of class Player: stop the current running instance of espeak.
  # If espeak is None or terminated, do nothing.
  # Called from the worker thread or the event processing thread.
  def stop_espeak(self):
    with self.lock:
      if self.espeak is not None and self.espeak.poll() is None:
        if self.paused:
          # Otherwise, terminate() won't do.
          self.espeak.send_signal(signal.SIGCONT)
        self.terminated_by_stop = True
        self.espeak.terminate()

  # A method of class Player: pause or resume the current running instance of espeak.
  # If espeak is None or terminated, do nothing.
  # Called from the worker thread or the event processing thread.
  def pause_espeak(self,pause_desired=True):
    with self.lock:
      if self.espeak is not None and self.espeak.poll() is None:
        try:
          self.espeak.send_signal(signal.SIGSTOP if pause_desired else signal.SIGCONT)
        except OSError as e:
          event_queue.put((P_ERR,_('Error while sending signal to espeak: %s.') % e))

  # A method of class Player: after reading one sentence, move to the next one. If already at the end
  # of the file and xpeak was called without --do-not-close-after-EOF, schedule xpeak to be terminated.
  # Always called from worker thread, with lock acquired.
  def advance(self):
    if self._stop_after_current_track or self._stop_after_each_line:
      self._stop_after_current_track = False
      say_stopped()
      if self.track < len(self.text)-1:
        self.go(self.track + 1)
      return False
    elif self.track < len(self.text)-1:
      self.go(self.track + 1)
      return True
    elif not args.do_not_close_after_EOF:
      event_queue.put((P_QUIT,'QUIT'))
      return False
    else:
      say_stopped()
      return False

  # A method of class Player.
  def stop(self,say_it = True):
    with self.lock:
      if not self.playing:
        return
      self.playing = False
      self.stop_espeak()
      # self.paused MUST be updated AFTER calling stop_espeak()
      self.paused = False
    # If the lock was already acquired before calling stop(), release it now to allow the worker thread
    # to die
    lock_level = 0
    while True:
      try:
        self.lock.release()
        lock_level += 1
      except RuntimeError:
        break
    self.worker_thread.join()
    # Then restore the lock
    for n in range(lock_level):
      self.lock.acquire()
    if say_it:
      say_stopped()

  # A method of class Player: pause/restart espeak
  def toggle(self):
    with self.lock:
      if self.load: # In case the file was modified while the player was paused
        if self.reload_file(): # The player was restarted from reload_file.
          return
      if self.playing:
        self.paused = not self.paused
        self.pause_espeak(self.paused)
      else:
        pause(args.pause_before)
        self.start()
        self.show_line()

  # A method of class Player: move line pointer forward
  def forward(self):
    with self.lock:
      b = self.playing and not self.paused
      track = self.track
    if track == len(self.text)-1:
      say(_('At the end of file'))
      return
    self.stop(False)
    self.go(track + 1)
    if b:
      self.start()

  msg_1st_line = _('At the beginning of file')
  msg_back     = _('back one line')

  # A method of class Player: move line pointer back
  def back(self,stop_playing):
    with self.lock:
      b = self.playing and not self.paused
      track = self.track
    if stop_playing:
      self.stop()
      if not b:
        if track == 0:
          say(self.msg_1st_line)
        else:
          say(self.msg_back)
          self.go(track - 1)
    elif track > 0:
      self.stop(False)
      say(self.msg_back)
      self.go(track - 1)
      if b:
        self.start()
    else:
      say(self.msg_1st_line)

  # A method of class Player: go to the first line
  def first(self,stop=True):
    with self.lock:
      track = self.track
    if track == 0:
      say(_('Already at the beginning of the file'))
    else:
      say(_('Back to first line'))
      self.stop(say_it = stop)
      self.go(0)
    if not stop:
      self.start()

  # A method of class Player: go to the last line
  def last(self,stop=True):
    with self.lock:
      track = self.track
      last_line = len(self.text) - 1
    if track == last_line:
      say(_('Already at the end of the file'))
    else:
      self.stop(say_it = stop)
      say(_('Forward to last line'))
      self.go(last_line)
    if not stop:
      self.start()

  # A method of class Player: read current sentence again
  def again(self):
    self.stop(False)
    self.start()

  # A method of class Player
  def stop_after_current_track(self):
    with self.lock:
      self._stop_after_current_track = not self._stop_after_current_track
      if self._stop_after_current_track:
        say(_('Player will stop after current track.'))
      else:
        say(_('Player will not stop after current track.'))

  # A method of class Player
  def stop_after_each_line(self):
    with self.lock:
      self._stop_after_each_line = not self._stop_after_each_line
      if self._stop_after_each_line:
        say(_('Player will stop after each line.'))
      else:
        say(_('Player will not stop after each line.'))

  # A method of class Player
  def change_speed(self,delta):
    if self.speed >= 400 and delta > 0 or self.speed <= 10 and delta < 0:
      say(_('Already at maximum speed') if delta > 0 else _('Already at minimum speed'))
      return
    with self.lock:
      b = self.playing and not self.paused
    self.stop(False)
    self.speed += delta
    say(_('%s words/min') % self.speed)
    if b:
      self.start()

  # A method of class Player: apply/do not apply substitutions
  def toggle_subst(self):
    with self.lock:
      line = self.line
      self.apply_subst = not self.apply_subst
      if self.apply_subst:
        say(_('Substitution rules are applied.'))
        load_subst()
      else:
        say(_('Substitution rules are not applied.'))
      self.update_line()
      if line != self.line:
        self.show_line()
        if self.playing:
          self.stop(say_it=False)
          self.start()

  # A method of class Player: show/do not show substituted text
  def toggle_show_subst(self):
    with self.lock:
      self.show_subst = not self.show_subst
      if self.show_subst:
        say(_('Effect of substitution rules is shown.'))
      else:
        say(_('Effect of substitution rules is not shown.'))
      self.show_line()

  # A method of class Player: show/do not show line numbers
  def toggle_line_numbers(self):
    with self.lock:
      self.show_line_numbers = not self.show_line_numbers
      self.show_line()

  # A method of class Player: an underlying monitored file changed on disk.
  def file_modified(self,action):
    if action in [monitored_file, splitting_rules]:
      if args.force_restart_after_change:
        self.reload_file(0)
      elif (self.playing and not self.paused) or args.always_reload_after_change:
        self.reload_file()
      else:
        say(_('File will be reloaded upon restart.'))
        with self.lock:
          self.load = True
          return
    elif action in subst_files and self.apply_subst:
      load_subst()
      if self.line != apply_subst(self.text[self.track]):
        self.restart(self.track)

  # A method of class Player: reload text. If restart_at == -1, compare new text with the old version
  # and restart only if the first modified line is before or at current line. If the text was shortened
  # before current line, stop the reading. If restart_at != -1, restart the reading at the specified
  # value, regardless of changes (in current version, only a value of 0 is given by file_modified).
  # Return True if the reading was restarted, False otherwise.
  def reload_file(self,restart_at = -1):
    tmp = load_text()
    if not tmp:
      say(_('Error while reloading file. Will continue as if not modified.'),muteable=False)
      return False
    with self.lock:
      self.old_text = self.text
      self.text = tmp
      text_len = len(self.text)
      self.load = False
    modified_lines = []
    if restart_at == -1:
      modified_lines = compare_text(self.text,self.old_text)
      restart_at = self.where_to_restart(modified_lines)
    if restart_at != -1:
      if restart_at < text_len:
        self.restart(restart_at)
      else:
        self.stop()
        self.go(text_len - 1)
        restart_at = -1
    self.show_changes(modified_lines)
    return restart_at != -1

  # A method of class Player: compare a list of modified lines with current track number and return
  # the number of the line where reading should restart, or -1 if no restart needed.
  def where_to_restart(self,modified_lines):
    if not modified_lines:
      return -1
    else:
      with self.lock:
        if modified_lines[0] <= self.track:
          return modified_lines[0]
    return -1

  # A method of class Player: restart reading at specified line.
  def restart(self,track):
    say(_('Restarting...'))
    b = self.playing and not self.paused
    self.stop(False)
    if b and locales[args.lang]['sam'] is not None:
      voice = args.voice or locales[args.lang]['voice']
      d = [ESPEAK,'-s',str(self.speed),'-v',voice,locales[args.lang]['sam']]
      subprocess.call(d)
    self.go(track)
    self.start()

  # A method of class Player: report modified lines on screen. Do not include the first line if it
  # matches the current track. Do not include changed lines after the end of the file (will be
  # reported as a shortening of the file).
  def show_changes(self,modified_lines):
    with self.lock:
      track = self.track
      newlen = len(self.text)
      oldlen = len(self.old_text)
    if not modified_lines and newlen == oldlen:
      say(_('No changes detected.'))
      return
    if track == modified_lines[0]:
      modified_lines = modified_lines[1:]
      msg = _('Change(s) also detected at line(s): %s; current line: %s.')
    else:
      msg = _('Change(s) detected at line(s): %s; current line: %s.')
    modified_lines = filter(lambda n : n < newlen,modified_lines)
    modified_lines = map(lambda i : str(i + 1),modified_lines)
    if modified_lines:
      say(msg % (', '.join(modified_lines),track + 1))
    if newlen < oldlen:
      say(_('The text was shortened from %s to %s line(s).') % (oldlen,newlen))
    elif newlen > oldlen:
      say(_('The text was extended from %s to %s lines.') % (oldlen,newlen))

# At long-last, the Player's definition ends here!


######################################################################################
# IMPLEMENTATION OF SUBSTITUTIONS

# Read the substitution files, create a list of substitution rules, apply as required.
# I implemented this outside the Player's class to avoid unnecessary cluttering.

subst_hist = []

def load_subst():
  global subst_match
  global subst_replace
  global subst_location

  subst_match    = []
  subst_replace  = []
  subst_location = []

  for fn in subst_files:
    if os.path.isfile(fn):
      try:
        with codecs.open(fn,'r',encoding) as f:
          s = f.read()
      except:
        say(_('Cannot open substitution file %s.') % fn,muteable=False)
        continue

      s = re.sub('(?m)^\s+$','',s)
      s = re.sub('(?m)^#.+?$','',s)
      s = re.sub('^\n+','',s)
      s += '\n\n'

      for rule in filter (lambda x : x , re.split('\n{2,}',s)):
        l = re.split('\n',rule)
        subst_match.append(l[0])
        subst_replace.append(l[1] if len(l) > 1 else '')
        subst_location.append(fn)


# Apply the substitution list to given text
def apply_subst(line):
  global subst_hist

  subst_hist = [(line,'','','')]

  for i in range(0,len(subst_match)):
    match    = subst_match[i]
    repl     = subst_replace[i]
    line_bak = line
    try:
      if not repl:
        line = re.sub(match,'',line,flags=re.U)
      else:
        line = re.sub(match,re.sub(r'\$(\d+)',lambda x : '\\' + x.group(1),repl),line,flags=re.U)

    except Exception as e:
      say(_('Wrong pattern, not applied: %s => %s') % (match,repl),muteable=False)
      say(str(e))

    line = re.sub(r'uc\((.+?)\)',lambda x : x.group(1).upper(),line)
    line = re.sub(r'lc\((.+?)\)',lambda x : x.group(1).lower(),line)
    line = re.sub(r'\\u(.)',lambda x : x.group(1).upper(),line)
    line = re.sub(r'\\l(.)',lambda x : x.group(1).lower(),line)

    if line_bak != line:
      subst_hist.append((line,subst_location[i],match,repl))

  return line


# Show the substitution history for current line
def print_subst_hist():
  if not player.apply_subst:
    return
  player.stop()
  if len(subst_hist) == 1:
    say(_('No substitutions were applied to this line.'),muteable=False)
  for n in range(0,len(subst_hist)):
    if subst_hist[n][1]:
      say(subst_hist[n][1],prompt=False,muteable=False,sep=False)
    if subst_hist[n][2]:
      say(subst_hist[n][2],prompt=False,muteable=False,sep=False)
    if subst_hist[n][3]:
      say(subst_hist[n][3],prompt=False,muteable=False,sep=False)
    say(subst_hist[n][0],prompt=False,muteable=False)


# Open substitution file (ask user which one to open)
def open_subst_file():
  player.stop(False)
  say(_('Choose substitution file to edit:'),muteable=False)
  n = 1
  for f in subst_files:
    s = str(n) + ': '
    if f.startswith(input_path + '/'):
      s += _('local')
    elif f.startswith(sys.path[0] + '/'):
      s += _('global')
    else:
      s += _('command')
    if not os.path.isfile(f):
      s += '(+)'
    s += '\t' + f
    say(s,prompt=False,muteable=False,sep=False)
    n += 1
  say('0: ' + _('back'),prompt=False,muteable=False)

  while True:
    c = getch()
    if c == '0':
      break
    elif re.match('\d',c) and int(c) >= 1 and int(c) <= len(subst_files):
      f = subst_files[int(c) - 1]
      if not os.path.isfile(f):
        try:
          with open(f,'w') as f2:
            f2.write('#' * 20 + '\n\n')
        except IOError:
          say(_('Cannot write to substitution file.'),muteable=False)
          break
      edit_file(f)
      break
  say_stopped()
  player.show_line()


######################################################################################
# SEARCHING
# This code was implemented outside the player class for two reasons: 1. to avoid cluttering the
# class definition, and 2. to keep all code that takes input directly from the outside world
# (standard input/the user) outside of the player definition.
# Code below should be mostly self-explanatory.

find_msg = {
  False: {
    False: _('Find (plain text; case insensitive)'),
    True : _('Find (plain text; case sensitive)')
  } ,
  True: {
    False: _('Find (regex text; case insensitive)'),
    True : _('Find (regex text; case sensitive)')
  }
}

find_what = None
find_re = None

def find(regex=False,cs=False):
  global find_what
  global find_re

  player.stop(False)

  say(find_msg[regex][cs]+ ':',muteable=False)

  restore_tty()

  what = unicode(raw_input(),encoding)

  # To separate the search string from the following output
  print(file=sys.stderr)

  if not what:
    player.show_line()
    init_tty()
    return

  find_what = what

  try:
    if not regex and not cs:
      find_re = re.compile(re.escape(find_what),re.U | re.I)
    elif not regex and cs:
      find_re = re.compile(re.escape(find_what),re.U)
    elif regex and not cs:
      find_re = re.compile(find_what,re.U | re.I)
    elif regex and cs:
      find_re = re.compile(find_what,re.U)
    _find_next(0)
  except re.error:
    say(_('Wrong pattern, not applied: %s') % find_what,muteable=False)
  finally:
    init_tty()


def find_next():
  _find_next(player.current_track() + 1)


def _find_next(where_from):
  if not find_re:
    return
  player.stop(False)

  text = player.get_text()

  for i in range (where_from,len(text)):
    if find_re.search(text[i]):
      say(_('Found: %s') % find_what,muteable=False)
      player.go(i)
      return
  say(_('Not found: %s') % find_what,muteable=False)


def find_last():
  if not find_re:
    return
  player.stop(False)

  text = player.get_text()

  for i in range (player.current_track() - 1 , -1 , -1):
    if find_re.search(text[i]):
      say(_('Found: %s') % find_what,muteable=False)
      player.go(i)
      return
  say(_('Not found: %s') % find_what,muteable=False)


######################################################################################
# MOVING TO A SPECIFIC LINE
# This code was implemented outside the player class for two reasons: 1. to avoid cluttering the
# class definition, and 2. to keep all code that takes input directly from the outside world
# (standard input/the user) outside of the player definition.
# Code below should be mostly self-explanatory.

def go_line():
  player.stop(False)

  say(_('Go to line:'),muteable=False,sep=True)

  restore_tty()

  what = unicode(raw_input(),encoding)

  # To separate the line number from the following output
  print(file=sys.stderr)

  if not what:
    player.show_line()
    init_tty()
    return

  if re.match('\d+',what):
    what = int(what)
    if what == 0 or what > len(player.get_text()):
      say(_('Wrong line.'),muteable=False)
    else:
      player.go(what - 1)
  else:
    say(_('Wrong line.'),muteable=False)

  init_tty()


######################################################################################
# PRINTING UTILITY
# A function to print to the console in a neatly way.
# - separates content with blank lines
# - adds a prompt before messages coming from xpeak (not text from the input file)
# - wraps input text within the screen
# - redirects ouptut to STDOUT or STDERR as needed
# - mutes muteable output when -q option was used
# - avoids printing the same line twice in a row (probably an unneeded legacy feature)

just_said = None

# what: the text to be written
# track: line number, if the text to be written comes from the target file
# prompt: print a > char before text, to indicate it's a status message from xpeak (not text
#         from the target file)
# sep: print an empty line after the text
# muteable: can be suppressed in quiet mode
# mandatory: must be printed always, even if it would be just repeating last printed line

def say(what,track=None,prompt=True,sep=True,muteable=True,mandatory=False):
  global just_said

  output = sys.stdout if muteable else sys.stderr

  if not prompt:
    if args.quiet >= 2 and muteable:
      return
    cols = int(subprocess.check_output(['stty','size']).split()[1])
  elif args.quiet >= 1 and muteable:
    return

  what = what.strip()
  if prompt:
    just_said = None
  elif str(track) + what == just_said and not mandatory:
    return
  else:
    just_said = str(track) + what

  if prompt:
    print('> ' + what,file=output)
  else:
    print('\n'.join(textwrap.wrap(what,cols)),file=output)
  if sep:
    print(file=output)


######################################################################################
# GLOBAL UTILITIES

# Start a function in a new daemon thread
def start_daemon(service):
  t = threading.Thread(target = service)
  t.daemon = True
  t.start()
  return t


# TODO: portability issue
# Read one character from stdin. This version of xpeak makes no use of non-printing keys (such as left
# arrow), so all of them are returned as a single ESC character
def getch():
  # This is a hack, which ensures all bytes comprising a single control key will be read in one call
  return os.read(sys.stdin.fileno(),42)[0]


# Implement a pause, letting the user continue with the keyboard. If the user presses the 'quit' key
# or introduces a quit command, ask for confirmation to terminate the program.
def pause(interval):
  if interval:
    say(_('paused: %ss') % interval)
    if len(select.select([sys.stdin],[],[],interval)[0]) > 0:
      if args.long_commands:
        cmd = sys.stdin.readline().strip()
      else:
        ch = getch()
        cmd = key_bindings[ch] if ch in key_bindings else None
    else:
      cmd = None
    if cmd in ['quit','QUIT']:
      if confirmation_to_quit(cmd):
        terminate()
      else:
        say(_('continuing'))


# Open a file in the preferred editing application
def edit_file(name):
  player.stop()
# TODO: portability issue here
  subprocess.Popen(['xdg-open',name])


def open_shell():
  player.stop()
  restore_tty()
  say(_('Entering the shell; close the shell to return to the program.'),muteable=False)
  subprocess.call([os.environ['SHELL']])
  init_tty()
  print(file=sys.stderr)
  say(_('Resuming the program.'),muteable=False)
  say_stopped()


def say_stopped():
  say(_('stopped'),muteable=False)


######################################################################################
# TERMINAL SETTINGS

old_tty_settings = None

# Set/reset standard input raw mode (unless xpeak was called with --long_commands)
def init_tty():
  global old_tty_settings
  if not args.long_commands:
    fd = sys.stdin.fileno()
    old_tty_settings = termios.tcgetattr(fd)
    new_tty_settings = termios.tcgetattr(fd)
    new_tty_settings[3] &= ~ (termios.ISIG | termios.ICANON | termios.ECHO)
    termios.tcsetattr(fd,termios.TCSADRAIN,new_tty_settings)


def restore_tty():
  if old_tty_settings is not None:
    termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_tty_settings)


######################################################################################
# PROGRAM ENTRY POINT

if __name__ == "__main__":
  main()
