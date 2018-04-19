#!/usr/bin/env python

# XUCW: a simple wrapper for unoconv
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

__version__='1.0.0'

import sys
import os
import argparse
import subprocess
import locale
import gettext

gettext.install('xucw',os.environ['TEXTDOMAINDIR'])

# TODO
encoding = locale.getpreferredencoding()

MAX_RETRIES = 10

def main():
  parser = argparse.ArgumentParser()
  parser.add_argument('infile',nargs='+')
  parser.add_argument('-f','--fmt',default='txt')
  args = parser.parse_args()

  for f in args.infile:
    if not os.path.isfile(f):
      sys.exit(_('File %s does not exist.') % f)
  ucargs = ['unoconv','-f',args.fmt,'--stdout','--timeout','60']
  ucargs.extend(args.infile)
  for n in range(MAX_RETRIES):
    try:
      print subprocess.check_output(ucargs,stderr=open('/dev/null','w')),
      break
    except subprocess.CalledProcessError:
      pass
      
if __name__ == '__main__':
  main()
