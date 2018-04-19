#!/usr/bin/env python

# XPLIT: a simple, configurable segmentation (sentence splitting) program
# (mainly) used as a companion to XPEAK
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
import re
import argparse
import locale
import re
import codecs

import gettext
gettext.install('xplit',os.environ['TEXTDOMAINDIR'])

lang, encoding = locale.getdefaultlocale()

def get_args():
  global args
  
  parser = argparse.ArgumentParser(prog='xplit.py')
  parser.add_argument('--version',action='version',version='%(prog)s ' + __version__)
  parser.add_argument('--lang',default=lang[0:2])
  parser.add_argument('-r','-R','--rules','--splitting-rules')
  parser.add_argument('-m','--mark','--html',action='store_true',default=False)
  parser.add_argument('-s','--strip-empty-lines',action='store_true',default=False)
  parser.add_argument('infile',nargs='?',default='-')
  parser.add_argument('outfile',nargs='?',default='-')
  args = parser.parse_args()
  if not args.rules or not os.path.isfile(args.rules):
    for p in [os.path.dirname(args.infile) or '.' , sys.path[0] ]:
      fn1 = p + '/xplit.rules'
      for fn2 in ['.' + args.lang,'']:
        if os.path.isfile(fn1 + fn2):
          args.rules = fn1 + fn2

def set_rules():
  global ds
  ds = []

  global ns
  ns = []

  fn = args.rules
  if fn and os.path.isfile(fn):
# TODO configure encoding within Rules file
    with codecs.open(fn,'r',encoding) as f:
      conv = f.read()
      conv = re.sub('^\s*#.+?$','',conv,flags = re.M)
      ds = re.findall(r'\s*<do>\s*<in>\s*(.+?)\s*</in>\s*<out>\s*(.+?)\s*</out>\s*</do>',conv)
      ns = re.findall(r'\s*<dont>\s*(.+?)\s*</dont>',conv)
  else:
    ds = [(r'([.!?])',r'\1\n')]
    ns = []
  
def main():
  get_args()
  set_rules()
  xplit()
  
def openwriter():
  if args.outfile == '-':
    return codecs.getwriter(encoding)(sys.stdout)
  return codecs.open(args.outfile,'w',encoding)

def openreader():
  if args.infile == '-':
    return codecs.getreader(encoding)(sys.stdin)
  return codecs.open(args.infile,'r',encoding)

def xplit():
  try:
    with openwriter() as outfile:
      with openreader() as infile:
        if (args.mark):
          outfile.write("""<!DOCTYPE html>
<html>
  <head>
    <meta charset="%s">'
  </head>
  <body>
""" % encoding)

        for line in infile:
          if (args.strip_empty_lines and not line.strip()):
            continue
          process(line,outfile)
        if (args.mark):
          outfile.write('  </body>\n</html>\n')
  except IOError as e:
    sys.exit(_('I/O Error: %s.') % e)

def repl(m,n,dnmw,dummy):
  dnmw.append(m.group())
  return dummy + str(n) + dummy
    
def process(line,outfile):
  dnmw = []

  if ns:
    dummy = '@'
    while dummy in line:
      dummy += '@'
    n = 0
    for r in ns:
      while True:
        line,matches = re.subn(r,lambda m : repl(m,n,dnmw,dummy),line,count=1,flags=re.UNICODE)
        if not matches:
          break
        n += matches

  for r in ds:
    line = re.sub(r[0],r[1],line)
  
  n = 0
  for s in dnmw:
    line = line.replace(dummy + str(n) + dummy,s)
    n += 1
  
  line = line.rstrip('\n')
  for s in line.split('\n'):
    if not args.mark:
      outfile.write(s + '\n')
    else:
      outfile.write('    <p>%s</p>\n' % (s))

if __name__ == '__main__':
  main()

