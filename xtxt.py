#!/usr/bin/env python

# XTXT: a command-line wrapper for printing different types of files to standard output
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

__version__ = '1.0.0'

import os
import sys
import argparse
import subprocess
import re
import locale
import codecs
import gettext

CONF_FILE = sys.path[0] + '/xtxt.cfg'

gettext.install('xtxt',os.environ['TEXTDOMAINDIR'])

# TODO
encoding = locale.getpreferredencoding()

args = None
conv = None

def main():
  global args
  global conv
  
  try:
    with open(CONF_FILE) as f:
      conv = f.read()
  except IOError:
    sys.exit(_('Cannot open configuration file %s.') % CONF_FILE)

  parser = argparse.ArgumentParser(prog='xtxt.py')
  parser.add_argument('--version',action='version',version='%(prog)s ' + __version__)
  parser.add_argument('infile',nargs='+')
  parser.add_argument('--lang',default='')
  args = parser.parse_args()

  for f in args.infile:
    if not os.path.isfile(f):
      sys.exit(_('File %s does not exist.') % f)
    else:
      process(f)
  
def mimetype(f):
  mt = subprocess.check_output(['mimetype', '-L',f])
  m = re.search(':\s+(\S+)\s*$',mt)
  return m.group(1) if m else None

def process(f):
  mt = mimetype(f)
  m = re.search('(?m)^mime:\s+%s\s*?^do:\s+(.+?)$' % mt,conv)
  if m:
    do = m.group(1)
  elif mt == 'text/plain':
    do = 'cat $file'
  else:
    ext = os.path.splitext(f)[1]
    if ext:
      m = re.search('(?m)^ext:\s+%s\s*^do:\s+(.+?)$' % ext[1:], conv)
      if m:
        do = m.group(1)
      else:
        sys.exit(_('No action configured for extension: %s') % ext[1:])
    else:
      sys.exit(_('No action configured for mimetype: %s') % mt)
  do = do.replace('$lang',args.lang)
  cmd = do.replace('$file',"'%s'" % f)
  run(cmd)

def getinputencoding():
  if encoding.lower() in ['utf-8','utf8']:
    return 'utf-8-sig'
  return encoding

def run(cmd):
  try:
    txt = unicode(subprocess.check_output(['sh','-c',cmd]),getinputencoding())
  except IOError:
    sys.exit(_('Cannot execute conversion command: %s.') % cmd)
  print txt,
    
if __name__ == '__main__':
  sys.stdout = codecs.getwriter(encoding)(sys.stdout)
  main()
