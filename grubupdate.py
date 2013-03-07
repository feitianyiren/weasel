#! /usr/bin/env python

###############################################################################
# Copyright (c) 2008-2009 VMware, Inc.
#
# This file is part of Weasel.
#
# Weasel is free software; you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free
# Software Foundation version 2 and no later version.
#
# Weasel is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License
# version 2 for more details.
#
# You should have received a copy of the GNU General Public License along with
# this program; if not, write to the Free Software Foundation, Inc., 51
# Franklin St, Fifth Floor, Boston, MA 02110-1301 USA.
#

#
# REMINDER: Python on ESXv3 is 2.2.3, so we need to be careful about not
# depending on any new stuff.
#

import os
import re
import sys
import stat
import time
import struct
import getopt
import shutil

TEMP_COMMENT = "# WEASEL -- "

GRUB_DIR = "/boot/grub"
GRUB_CONF = os.path.join(GRUB_DIR, "grub.conf")
GRUB_CONF_PREV = GRUB_CONF + ".esx3"

UPGRADE_DIR = "/esx4-upgrade"

DEVICE_MAP = os.path.join(UPGRADE_DIR, "device.map")

ISO_LOC_CDROM = "cdrom"
ISO_LOC_FILE = "file"

AGENT_INIT_PATH = "/tmp/agent-init.sh"

# List of files to append to the initrd.img
INITRD_EXTRA_FILES = [
    "etc/hosts",
    "etc/resolv.conf",
    "etc/vmware/ks-upgrade.cfg",
    "etc/vmware/esx.conf",
    "etc/sysconfig/network",
    "etc/sysconfig/network-scripts/ifcfg-vswif*",
    ]

# XXX The "title" must be the first line in here since we use it to remove
# titles from any previous runs.
UPGRADE_TITLE = """title Upgrade ESX
\troot %(roothd)s
\tuppermem 524288
\tkernel %(kernelPath)s mem=512M upgrade bootpart=%(bootUUID)s rootpart=%(rootUUID)s ks=file:///etc/vmware/ks-upgrade.cfg source='%(isoPathQuoted)s'
\tinitrd %(initrdPath)s
"""

DEVICE_MAP_TEMPLATE = """\
# this device map was generated by grubupdate.py
(hd0) %s
"""

LOG_PATH = "/var/log/grubupdate.log"
LOG_FILE = None

def usage():
    print "usage: %s [-h] <iso-location> <kickstart-file> [agent [agent-arg0 ...]]" % (
        sys.argv[0])
    print
    print "Update the GRUB configuration to reboot into the installer for an"
    print "upgrade."
    print
    print "Required Arguments:"
    print "  iso-location     The location of the installer's ISO in the form:"
    print "                   (cdrom|file://<path>)."
    print "  kickstart-file   The path to the upgrade kickstart file."
    print "  agent            The path to the monitoring agent to run in"
    print "                   the install environment (must be in a directory"
    print "                   already in the initrd).  Any extra arguments are"
    print "                   are passed to the agent on startup."
    print
    print "Example:"
    print "  $ %s file:///vmfs/volumes/storage1/installer.iso /ks.cfg"

def shquote(arg):
    '''Escape single quotes in a string that will be used in a shell command.'''
    return arg.replace("'", "'\"'\"'")

def log(msg):
    # No logging module in python v2.2
    global LOG_FILE

    if not LOG_FILE:
        return
    stampedMsg = "%s: %s" % (time.asctime(), msg)
    LOG_FILE.write(stampedMsg)
    sys.stderr.write(msg)

def parseISOLocation(isoLocation):
    '''Parse the iso location url-thingy as given on the command line and return
    a path to the ISO file.

    For the CD-ROM case, we can just return the device itself since the isoinfo
    command works just fine with that.

    >>> parseISOLocation("cdrom")
    '/dev/cdrom'
    >>> parseISOLocation("file:///vmfs/volumes/Storage 1/installer.iso")
    '/vmfs/volumes/Storage 1/installer.iso'
    '''
    m = re.match(r'(cdrom|file:/?/?(/.+))', isoLocation)
    if not m:
        return None

    if m.group(1) == 'cdrom':
        return "/dev/cdrom" # XXX Can we depend on this?
    else:
        return m.group(2)

def uniqueFilename(path):
    '''Find a unique filename by starting with the given base path and then
    appending a number until a unique name is found.'''
    
    origPath = path
    counter = 0
    while os.path.exists(path):
        path = "%s.%d" % (origPath, counter)
        counter += 1
    
    return path

def ensureLink(path):
    '''If the file is not already a symbolic link, make it one.

    The default anaconda install creates "menu.lst" as a link to "grub.conf",
    however, if the user changes that, we need to set it back without destroying
    their changes.
    '''

    if not os.path.islink(path):
        bakname = uniqueFilename(path)
        os.rename(path, bakname)
        os.symlink(os.path.basename(bakname), path)

def removeTitle(grubConf, title):
    '''Remove a title from a grub.conf.

    >>> print removeTitle("title foo\n\tkernel foo\ntitle bar\n", "title foo")
    title bar
    <BLANKLINE>
    >>> print removeTitle("title bar\n", "title foo")
    title bar
    <BLANKLINE>
    '''
    oldMatch = re.search(r'^%s$' % re.escape(title), grubConf, re.MULTILINE)
    if oldMatch:
        start = oldMatch.start()
        nextMatch = re.search(r'^[ \t]*title\b',
                              grubConf[oldMatch.end():],
                              re.MULTILINE)
        if nextMatch:
            end = oldMatch.end() + nextMatch.start()
        else:
            end = len(grubConf)
        return grubConf[0:start] + grubConf[end:]

    return grubConf

def insertTitle(grubConf, titleBody):
    '''Insert a title into a grub.conf file.

    >>> print insertTitle("timeout=10\ntitle foo\nroot (hd0,0)\n",
    ...                   "title bar\n")
    timeout=10
    title bar
    title foo
    root (hd0,0)
    <BLANKLINE>
    >>> print insertTitle("timeout=10\n", "title bar\n")
    timeout=10
    title bar
    <BLANKLINE>
    '''

    titleMatch = re.search(r"^[ \t]*title\b", grubConf, re.MULTILINE)
    if titleMatch:
        titleStart = titleMatch.start(0)
    else:
        titleStart = len(grubConf)

    return grubConf[0:titleStart] + titleBody + grubConf[titleStart:]

def splitPath(path):
    '''Split a /dev device path into the path for the whole device and the
    partition number.

    >>> splitPath("/dev/sda10")
    ('/dev/sda', 10)
    >>> splitPath("/dev/cciss/c0d0p1")
    ('/dev/cciss/c0d0', 1)
    >>> splitPath("/dev/sx8/0p2")
    ('/dev/sx8/0', 2)
    '''

    m = re.match(r'(/dev/(?:cciss|rd|sx8|ida)/[^p]+|'
                 # The part above ^^^ matches the device path up to the
                 # partition number when the path has an intervening directory
                 # (e.g. cciss).
                 r'/dev/[^\d]+)' # Match 'normal' devices.
                 r'p?(\d+)', # Finally, capture the partition number.
                 path)
    assert m
    assert len(m.groups()) == 2
    
    return (m.group(1), int(m.group(2)))

def makeDeviceMap():
    '''Make a device.map file for grub that maps the boot partition to hd0.'''

    bootPartPath = findDeviceForPath("/boot")
    if not bootPartPath:
        log("error: cannot find boot device\n")
        return False

    bootDevPath, _bootPartNum = splitPath(bootPartPath)

    log("info: boot device -- %s (%s)\n" % (bootDevPath, bootPartPath))
    
    devMapFile = open(DEVICE_MAP, "w")
    try:
        devMapFile.write(DEVICE_MAP_TEMPLATE % bootDevPath)
    finally:
        devMapFile.close()

    return True

def findRoot(kernelPath):
    '''Use grub to find a file on a hard drive and return the grub name for the
    hard drive.'''

    # Don't remake the device.map in case the user needs to modify it.
    if not os.path.exists(DEVICE_MAP) and not makeDeviceMap():
        return None

    cmd = "/sbin/grub --batch --device-map=%s" % DEVICE_MAP
    
    (childStdin, childStdout) = os.popen2(cmd)

    childStdin.write("find %s\n" % kernelPath)
    childStdin.close()

    grubSpew = childStdout.read()
    childStdout.close()

    log("info: BEGIN grub output\n%s\ninfo: END grub output\n" % grubSpew)

    m = re.search(r'(\(hd.+\))', grubSpew)
    if m:
        retval = m.group(1)
    else:
        retval = None
    
    return retval

def changeSetting(grubConf, settingName, newValue):
    '''Change a setting in a grub.conf header to a new value.

    >>> print changeSetting("# Hi\n", "default", "saved")
    default saved
    # Hi
    <BLANKLINE>
    >>> print changeSetting("default 1\n", "default", "saved")
    # WEASEL -- default 1
    default saved
    <BLANKLINE>
    >>> print changeSetting("\ntimeout=1\n", "timeout", "10")
    <BLANKLINE>
    # WEASEL -- timeout=1
    timeout 10
    <BLANKLINE>
    '''

    reObj = re.compile(r'^([ \t]*%s\b.*\n)' % settingName, re.MULTILINE)

    retval = reObj.sub(r'%s\1%s %s\n' % (TEMP_COMMENT, settingName, newValue),
                       grubConf)
    if retval == grubConf:
        retval = ("%s %s\n" % (settingName, newValue)) + retval

    return retval

def _findDevVisitor((devid, accum), dirname, names):
    for name in names:
        if name.startswith('pty') or name.startswith('tty'):
            continue
        
        path = os.path.join(dirname, name)
        try:
            st = os.stat(path)
            if not stat.S_ISBLK(st.st_mode):
                # Only consider block devices.
                # XXX st_rdev should be zero for non-device nodes, but that
                # doesn't always seem to be the case, not sure why.
                continue
            
            if devid == st.st_rdev:
                accum.append(path)
                return
        except OSError:
            log("warn: cannot stat %s\n" % path)

def findDeviceForPath(path):
    '''Find the device that a given file sits on and return the path for the
    device.
    '''
    
    pathStat = os.stat(path)
    deviceMatch = []

    os.path.walk('/dev', _findDevVisitor, (pathStat.st_dev, deviceMatch))
    if deviceMatch:
        return deviceMatch[0]
    return None

def _uuidBitsToString(uuidBits):
    retval = []
    # XXX I'm sort of abusing unpack to break up the bits into a list.  The
    # unpack will create a list of bytes split into five groups that will be
    # formatted in hex, like so:  e8b6d3d1-5204-4c47-b312-a625a97cf30d
    for subBytes in struct.unpack("4s2s2s2s6s", uuidBits):
        # Convert the unpacked bytes into a hex string.
        retval.append("".join(
                ["%02x" % ord(byte) for byte in subBytes]))
    return "-".join(retval)

EXT_MAGIC1 = 0123
EXT_MAGIC2 = 0357

def uuidForDevice(devName):
    try:
        # udev/extras/volume_id/volume_id/ext.c
        
        superBlockOffset = 0x400
        superBlockFormat = 'IIIIIII7IBBH8IIII16s16s'
        superBlockSize = struct.calcsize(superBlockFormat)

        devFile = open(devName)
        try:
            devFile.seek(superBlockOffset)
            headers = devFile.read(superBlockSize)
        finally:
            devFile.close()

        superBlock = struct.unpack(superBlockFormat, headers)

        magic1 = superBlock[14]
        magic2 = superBlock[15]

        if magic1 != EXT_MAGIC1 or magic2 != EXT_MAGIC2:
            log("error: cannot find ext2 superblock on %s\n" % devName)
            return None

        uuid = superBlock[28]

        if uuid == ("\0" * len(uuid)):
            log("error: no UUID on ext2 partition %s\n" % devName)
            return None
    
        return _uuidBitsToString(uuid)
    except (struct.error, ValueError), e:
        log("error: cannot get UUID for %s -- %s\n" % (devName, str(e)))

    return None

def deviceUUIDForPath(path):
    devicePath = findDeviceForPath(path)
    if not devicePath:
        log("error: cannot find device for %s\n" % path)
        return None

    uuid = uuidForDevice(devicePath)
    if not uuid:
        log("error: cannot get UUID for %s\n" % devicePath)
        return None

    log("info: %s -> %s -> %s\n" % (path, devicePath, uuid))

    return uuid

def extractBootFiles(isoPath):
    if not os.path.exists(UPGRADE_DIR):
        try:
            os.makedirs(UPGRADE_DIR)
        except OSError, e:
            log("error: cannot create upgrade directory -- %s\n" %
                             str(e))
            return None

    isoinfoPath = os.path.join(UPGRADE_DIR, "isoinfo")

    kernelPath = os.path.join(UPGRADE_DIR, "vmlinuz")
    rc = os.system("%s -x '/ISOLINUX/VMLINUZ.;1' -i '%s' > %s" %
                   (isoinfoPath, shquote(isoPath), kernelPath))
    if rc:
        log("error: cannot extract vmlinuz from ISO\n")
        return None

    initrdPath = os.path.join(UPGRADE_DIR, "initrd.img")
    rc = os.system("%s -x '/ISOLINUX/INITRD.IMG;1' -i '%s' > %s" %
                   (isoinfoPath, shquote(isoPath), initrdPath))
    if rc:
        log("error: cannot extract initrd.img from ISO\n")
        return None

    return (kernelPath, initrdPath)

def appendConfToInitrd(ksPath, agentArgs, initrdPath):
    '''Append the kickstart, esx.conf and related files to the initrd.img

    The kernel is able to handle a single initrd.img that is made up of multiple
    gzipped cpio files concatenated together.  Appending the files allows us to
    get around any limitations with bringing up storage on the other side.
    '''
    
    # XXX directories in the appended file don't show up for some reason, so we
    # copy the kickstart file to a file that exists in the original initrd.
    shutil.copy(ksPath, "/etc/vmware/ks-upgrade.cfg")

    extraFiles = list(INITRD_EXTRA_FILES)
    if agentArgs:
        agentFile = open(AGENT_INIT_PATH, 'w')
        agentFile.write("#! /bin/sh\nchmod ugo+rx %s\n%s\n" % (
            agentArgs[0], " ".join(agentArgs)))
        agentFile.close()
        
        extraFiles += [AGENT_INIT_PATH, agentArgs[0]]
    
    rc = os.system("cd / && ls %s | "
                   "/bin/cpio --format=newc -o | "
                   "/bin/gzip >> %s" % (
        " ".join(extraFiles), initrdPath))

    return rc == 0

def main(argv):
    global LOG_FILE
    
    LOG_FILE = open(LOG_PATH, "a")

    log("info: starting -- %s\n" % str(argv))
    
    try:
        (opts, args) = getopt.getopt(argv[1:], "h", ["help"])
        
        for opt, arg in opts:
            if opt in ("-h", "--help"):
                usage()
                return 0
            
        if len(args) < 2:
            log("error: expecting ISO and kickstart paths\n")
            return 1

        isoLocation, ksPath = args[:2]
        
        isoPath = parseISOLocation(isoLocation)
        if not isoPath:
            log("error: invalid ISO location -- %s\n" % isoLocation)
            return 1
        
        agentArgs = None
        if len(args) > 2:
            agentArgs = args[2:]
            badArgs = filter(lambda arg: not re.match(r'[\w\-\.:,@/]+', arg),
                             agentArgs)
            if badArgs:
                log("error: invalid upgrade agent argument(s) -- %s\n" %
                    " ".join(badArgs))
                return 1
    except getopt.error, e:
        log(str(e))
        return 1
    
    if not os.path.exists(isoPath):
        log("error: ISO does not exist -- %s\n" % isoPath)
        return 1

    ksPath = os.path.abspath(ksPath)
    if not os.path.exists(ksPath):
        log("error: kickstart does not exist -- %s\n" % ksPath)
        return 1

    if agentArgs and not agentArgs[0].endswith("vua-standin.sh") and \
            not os.path.exists(agentArgs[0]):
        log("error: invalid agent path -- %s\n" % agentArgs[0])
        return 1

    paths = extractBootFiles(isoPath)
    if not paths:
        return 1

    isoPathQuoted = shquote(isoPath)

    kernelPath, initrdPath = paths
    assert kernelPath
    assert initrdPath

    if not appendConfToInitrd(ksPath, agentArgs, initrdPath):
        log("error: could not append extra files to initrd\n")
        return 1

    roothd = findRoot(kernelPath)
    if not roothd:
        log("error: grub cannot find root hd number\n")
        return 1

    uuids = [deviceUUIDForPath(path) for path in ("/", "/boot")]
    if None in uuids:
        return 1

    rootUUID, bootUUID = uuids

    oldconfFile = open(GRUB_CONF, 'r')
    oldconf = oldconfFile.read()
    oldconfFile.close()

    newTitle = UPGRADE_TITLE % locals()

    newconf = removeTitle(oldconf, newTitle.split('\n')[0])
    newconf = insertTitle(newconf, newTitle)
    newconf = changeSetting(newconf, "default", "0")
    # Need to make sure it will automatically boot but we still want to let them
    # pick one of the other options.
    newconf = changeSetting(newconf, "timeout", "5")
    
    newconfName = uniqueFilename(os.path.join(GRUB_DIR, "upgrade.grub.conf"))
    newconfFile = open(newconfName, 'w')
    newconfFile.write(newconf)
    newconfFile.close()

    if not os.path.exists(GRUB_CONF_PREV):
        shutil.copy(GRUB_CONF, GRUB_CONF_PREV)

    ensureLink(GRUB_CONF)
    os.remove(GRUB_CONF)
    os.symlink(os.path.basename(newconfName), GRUB_CONF)

    return

if __name__ == "__main__":
    sys.exit(main(sys.argv))
