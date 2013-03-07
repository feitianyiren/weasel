#! /usr/bin/python

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

'''
Functions to configure and install the bootloader (GRUB)

When working with this module, you should keep the following in mind, there
are 4 contexts you have to keep track of:
 1. installer environment (for regular commands)
 2. chroot environment (during install, for commands chrooted to /mnt/sysimage)
 3. GRUB boot-up environment (post-install, when the BIOS hands off to GRUB)
 4. GRUB shell environment (during install, disk mapping comes from device.map)
'''

import os
import re
import shutil
import glob
import errno
import xml.dom.minidom

import consts
import devices
import packages
import workarounds
import userchoices
import partition
import vmkctl
import migrate

from grubupdate import insertTitle, removeTitle, changeSetting, GRUB_CONF_PREV
from log import log
from util import execCommand, \
                 XMLGetTextInUniqueElement, \
                 getUuid, syncKernelBufferToDisk


# -------- CONTENT OF THE CONF FILES --------

grub_conf_header = '''\
###################### grub.conf #####################
# this file was generated by bootloader.py
#
# Any entries in this file marked with the comment
#   #vmware:autogenerated esx
# Should not be edited by hand.  They are likely to
# be clobbered on or before the next reboot.
#
timeout=5
%(passwordLine)s
'''

# NOTE: the "root" command will always point to disk hd0 because we only
# support putting the kernel on the first (according to BIOS order) disk
grub_conf_entry = '''\
title %(label)s
	#vmware:autogenerated esx
	root (hd0,%(slashBootPartNum)d)
	uppermem %(upperMemInKB)s
	kernel %(bootDirRelativeToGrub)s%(kernel_file)s %(kernelOptions)s quiet
	initrd %(bootDirRelativeToGrub)s%(initrd_file)s
'''

device_map = '''\
###################### device.map #####################
# this file was generated by bootloader.py
# it is not needed after /usr/sbin/grub has been run
#
# (hd0) in this context doesn't mean "first disk" it's
# just a pointer to the /dev/whatever disk, so the
# "0" component of (hd0) is functionally arbitrary
(hd0) %(installerDevDiskName)s
'''

sysconfig_grub = '''\
###################### sysconfig/grub #####################
# this file was generated by bootloader.py
# it is read by mkinitrd
#

boot=%(devPartName)s

#
# forcelba: some BIOSes mis-inform grub by not supplying a
#           correct LBA support bitmap, even when they do
#           have the support for LBA.  Using forcelba tells
#           grub to ignore the misinformation.
forcelba=0
'''


# -------- FILE NAMES -------

instRoot = consts.HOST_ROOT
grub_dir            = instRoot + 'boot/grub/'
grub_conf_file      = grub_dir + 'grub.conf'
sysconfig_grub_file = instRoot + 'etc/sysconfig/grub'
device_map_file     = '/tmp/device.map' #temp file for /usr/sbin/grub
kernel_file         = 'vmlinuz'
initrd_file         = 'initrd.img'
grub_stage_img_dir  = instRoot + 'usr/share/grub/x86_64-redhat/'

# -------- COMMANDS --------

cmd_esxcfgboot = '/usr/sbin/esxcfg-boot'

# grub_exec_cmd
# "root BLAH": specifies what partition to find the stage1 file
#              using (hd0,0) just means "use the first partition of whichever 
#              device was called (hd0) in device_map_file
# "setup BLAH" actually installs grub, using images found on the specified disk
cmd_grub = '/usr/sbin/grub --batch --device-map='+ device_map_file +''' <<EOF
root (hd0,%(slashBootPartNum)s)
setup %(grubInstallLocation)s
quit
EOF'''



class PartitionNotFound(Exception):
    def __init__(self, partname):
        Exception.__init__(self, 'No %s partition has been defined' % partname)

class MissingConsoleDevicePath(Exception):
    def __init__(self, partition):
        Exception.__init__(self, '''\
        Partition (%s) is missing a console device path.  This may indicate
        that the partition requests have not yet been fitted to the disk.
        ''' % partition.name)

class GrubCheckException(Exception):
    def __init__(self, msg):
        Exception.__init__(self, msg)
        self.msg = msg


#------------------------------------------------------------------------------
def getKernelOptions(kernelMemInMB):
    '''Create the list of options that get appended onto the "kernel"
    line of the grub.conf file.
    '''
    disks = devices.DiskSet()
    rootPartition = disks.findPartitionContaining('/', searchVirtual=True)
    if not rootPartition:
        raise PartitionNotFound('/')

    devPartName = rootPartition.consoleDevicePath
    if not devPartName:
        # expected /dev/sda1 for the 1-disk case
        raise MissingConsoleDevicePath(rootPartition)
    rootPartUuid = getUuid(devPartName)

    # ro: mount "/" read-only in the initrd environment but when the init 
    # scripts start up it magically gets remounted rw.
    options = 'ro'

    # root: tell the kernel where to find "/"
    options += ' root=UUID='+ rootPartUuid

    # mem: tell the kernel how much memory it can use
    options += ' mem='+ kernelMemInMB +'M'

    choices = userchoices.getBoot()
    if choices and choices['kernelParams']:
        options += ' '+ choices['kernelParams']
    return options

#------------------------------------------------------------------------------
def getPasswordLine():
    '''Create the (optional) "password" line of the grub.conf file.
    [password [--md5] PASSWD]
    '''
    passwordLine = ''
    password = ''

    choices = userchoices.getBoot()
    if choices:
        password = choices['password']

    if (password
        and choices['passwordType'] == userchoices.BOOT_PASSWORD_TYPE_MD5):
        passwordLine = 'password --md5 '+ password
    elif password:
        passwordLine = 'password '+ password
    return passwordLine

#------------------------------------------------------------------------------
def getKernelLabelAndVersion():
    ''' Grab the kernel_name and kernel_version from the packages.xml file '''
    packagesXML = packages.getPackagesXML(None)

    kernLabel = packagesXML.kernLabel.encode('ascii')
    kernVersion = packagesXML.kernVersion.encode('ascii')

    return kernLabel, kernVersion

#------------------------------------------------------------------------------
def findGrubInstallDevice(slashBootPartition):
    '''Find the device (the disk) to install grub onto (write the MBR).
    In the future, this might return something other than the disk
    containing the /boot partition.  For example, a userchoices attribute
    may override the selection, or the policy might change to
    "the first device in GetOrderedDrives()".
    '''
    disks = devices.DiskSet()
    disk = disks.findDiskContainingPartition(slashBootPartition)
    assert disk, "Just found /boot partition, but then it's disk vanished!"
    return disk


#------------------------------------------------------------------------------
def grubDiskAndPartitionIndicies(part):
    '''Grub defines disks and partitions by their indicies as they are found
    in the boot order.  It has no knowledge of sdX versus hdX versus CCISS
    versus DAC960.  It just knows the first, second, third, etc. and expects
    them to be indicated in its conf files in a format like (hd0,0)
    So this function, for a given partition, gets the index of the disk and
    the index of the partition on said disk.

    Raise a KeyError if it can not be found in the DiskSet.
    '''
    disks = devices.DiskSet()
    orderedDrives = disks.getOrderedDrives()

    for diskIndex, drive in enumerate(orderedDrives):
        for candidatePart in drive.partitions:
            if candidatePart == part:
                # logical partitions ALWAYS start at 4, so use partitionId
                grubPartitionNum = candidatePart.partitionId-1
                log.debug('Disk/partition %s/%s enumerated as %d,%d '
                          'for grub' % (drive.name, candidatePart.name,
                                        diskIndex, grubPartitionNum))
                return (diskIndex, grubPartitionNum)

    nonStandardDisk = disks.findDiskContainingPartition(part)
    if nonStandardDisk:
        # XXX this should disappear...
        # logical partitions ALWAYS start at 4, so use partitionId
        grubPartitionNum = part.partitionId-1
        log.debug('Non-standard disk/partition 0/%d' % (grubPartitionNum))
        return (0, grubPartitionNum)

    partName = part.getName()
    raise KeyError('Partition not found in disk set: %s' % partName)

#------------------------------------------------------------------------------
def grubDiskIndex(disk):
    '''Operates like grubDiskAndPartitionIndicies but it only cares about
    the disk index.
    '''
    disks = devices.DiskSet()
    orderedDrives = disks.getOrderedDrives()

    for diskIndex, candidateDisk in enumerate(orderedDrives):
        if candidateDisk == disk:
            return diskIndex

    raise KeyError('Disk not found in disk set: %s' % disk.name)
                
#------------------------------------------------------------------------------

def getStringSubstitutionDict():
    label, version = getKernelLabelAndVersion()

    # Get the kernel mem to put in the boot command line.
    kernelMemInMB = vmkctl.MemoryInfoImpl().GetServiceConsoleReservedMem()
    upperMemInKB = str(kernelMemInMB*1024)

    disks = devices.DiskSet()

    if userchoices.getUpgrade():
        # We reuse the boot partition from the old install.
        bootPath = os.path.join(consts.ESX3_INSTALLATION, "boot")
    else:
        bootPath = "/boot"
    slashBootPartition = disks.findPartitionContaining(bootPath)
    if not slashBootPartition:
        raise PartitionNotFound(bootPath)

    devPartName = slashBootPartition.consoleDevicePath
    if not devPartName:
        raise MissingConsoleDevicePath(slashBootPartition)
    slashBootPartUuid = getUuid(devPartName)

    diskIndex, partIndex = grubDiskAndPartitionIndicies(slashBootPartition)
    if diskIndex != 0:
        log.warn('Installing GRUB to the MBR of a disk that was not the first'
                 ' disk reported by the BIOS.  User must change their BIOS'
                 ' settings if they want to boot from this disk')
    slashBootPartNum = partIndex

    grubInstallDevice = findGrubInstallDevice(slashBootPartition)

    # This code protects against instability with the /vmfs partition
    # going missing.  We try to use the canonical path first, however if it
    # isn't there, use the /dev/sdX path and warn.  The variable is used
    # in device.map.

    if os.path.exists(grubInstallDevice.path):
        installerDevDiskName = grubInstallDevice.path
    elif os.path.exists(grubInstallDevice.consoleDevicePath):
        log.warn('The normal path to the boot disk did not exist. '
                 'Using console device path instead.')
        installerDevDiskName = grubInstallDevice.consoleDevicePath
    else:
        raise RuntimeError, "Couldn't find a place to write GRUB."

    # decide between installing to the MBR or the first partition
    bootChoices = userchoices.getBoot()
    if bootChoices and \
       bootChoices['location'] == userchoices.BOOT_LOC_PARTITION:
        grubInstallLocation = '(hd0,%s)' % slashBootPartNum
        log.info('Installing GRUB to the first partition of %s ' %
                 installerDevDiskName)
    else:
        grubInstallLocation = '(hd0)'
        log.info('Installing GRUB to the MBR of (%s)' % installerDevDiskName)

    # need to tell grub.conf where it will find the kernel and initrd
    # relative to the root of the partition it searches.
    if slashBootPartition.mountPoint in [
        '/boot', os.path.join(consts.ESX3_INSTALLATION, "boot")]:
        bootDirRelativeToGrub = '/'
    elif slashBootPartition.mountPoint in ['/', consts.ESX3_INSTALLATION]:
        bootDirRelativeToGrub = '/boot/'

    kernelOptions = getKernelOptions(str(kernelMemInMB))

    passwordLine = getPasswordLine()


    substitutes = dict(
                       label = label,
                       version = version,
                       devPartName = devPartName,
                       kernel_file = kernel_file,
                       initrd_file = initrd_file,
                       passwordLine = passwordLine,
                       upperMemInKB = upperMemInKB,
                       kernelMemInMB = kernelMemInMB,
                       kernelOptions = kernelOptions,
                       slashBootPartNum = slashBootPartNum,
                       slashBootPartUuid = slashBootPartUuid,
                       grubInstallLocation = grubInstallLocation,
                       installerDevDiskName = installerDevDiskName,
                       bootDirRelativeToGrub = bootDirRelativeToGrub,
    )
    return substitutes


#------------------------------------------------------------------------------
def copyGrubStageImages():
    '''make sure all the "stage" images are stored on the boot
    partition.  
    This replaces `/sbin/grub-install --just-copy` that was in anaconda
    '''

    choices = userchoices.getBoot()
    if choices and choices['doNotInstall']:
        log.info('Skipping the writing of the bootloader to disk')
        return
    
    if not os.path.exists(grub_dir):
        os.makedirs(grub_dir) #makes directory for the stage images

    stageImgFilenames = glob.glob(grub_stage_img_dir + '*')
    if not stageImgFilenames:
        raise Exception('grub package directory (%s) was empty!'
                        % grub_stage_img_dir)
    for fname in stageImgFilenames:
        basename = os.path.basename(fname)
        destination = grub_dir + basename
        shutil.copy(fname, destination)


#------------------------------------------------------------------------------
def makeBackups():
    '''Backup old grub configuration files files'''
    for fname in [ grub_conf_file, 
                   sysconfig_grub_file, 
                 ]:
        try:
            shutil.copy(fname, fname + '.old')
        except IOError, ex:
            if ex.errno == errno.ENOENT: #file not found error
                log.info('File %s was not present.  No backup made' % fname)
                pass
            else:
                raise


#------------------------------------------------------------------------------
def parseGrubConf(grubConf):
    '''Parse a grub.conf file into a list of dictionaries for each title.'''
    retval = []

    currentTitle = None
    for line in grubConf.split('\n'):
        if line.lstrip().startswith('title '):
            currentTitle = { 'body' : '' }
            retval.append(currentTitle)
        if currentTitle is not None:
            currentTitle['body'] += "%s\n" % line
            key = line.strip().split(' ')[0]
            currentTitle[key] = line

    return retval
    
#------------------------------------------------------------------------------
def writeUpgradeFiles(grubContents):
    def ensureBootPrefix(path):
        if not path.startswith("/boot"):
            path = os.path.join("/boot", path.lstrip('/'))
        return path
    
    titlesToRemove = []
    pathsToRemove = set()

    # Loop through the grub.conf looking for old ESX v3 titles.
    for title in parseGrubConf(grubContents):
        if 'kernel' not in title:
            # Title has no kernel to boot?
            continue
        
        m = re.search(r'root=UUID=(\S+)', title['kernel'])
        if not m:
            # Cannot find root partition UUID
            continue

        if m.group(1) != userchoices.getRootUUID()['uuid']:
            # Does not match the 3.x root partition UUID.
            continue
        
        titlesToRemove.append(title)
        
        m = re.search(r'/vmlinuz-([^\s]+)', title['kernel'])
        if m:
            path = m.group(1)
            pathsToRemove.add(ensureBootPrefix("vmlinuz-%s" % path))
            pathsToRemove.add(ensureBootPrefix("vmlinux-%s" % path))
            pathsToRemove.add(ensureBootPrefix("config-%s" % path))
            pathsToRemove.add(ensureBootPrefix("System.map-%s" % path))
            pathsToRemove.add(ensureBootPrefix("System.map"))
            pathsToRemove.add(ensureBootPrefix("kernel.h"))

        m = re.search(r'(/[^\s]+)', title['initrd'])
        if m:
            path = m.group(1)
            pathsToRemove.add(ensureBootPrefix(path))

    header = "# BEGIN ESX v3 title"
    footer = "# END ESX v3 title"
    for title in titlesToRemove:
        body = title['body'].strip()
        grubContents = grubContents.replace(
            body, "%s\n%s\n%s\n" % (header, body, footer), 1)

    fp = open(migrate.CLEANUP_PATH, 'a')
    fp.write("\n# Remove old ESX v3 titles in grub.conf\n"
             "sed -i -e '/^%s/,/^%s/d' /boot/grub/grub.conf\n" % (
            header, footer))
    if pathsToRemove:
        fp.write("\n# Remove old ESX v3 boot files:\n")
        for path in pathsToRemove:
            fp.write("rm -f %s\n" % path)
    fp.write("rm -f /usr/sbin/cleanup-esx3")
    fp.close()

    return grubContents
    
#------------------------------------------------------------------------------
def writeGrubConfFiles(stringSubstitutionDict):
    '''make grub config files'''
    # make sure the expected directories exist
    if not os.path.exists(grub_dir):
        os.makedirs(grub_dir)
    if not os.path.exists(os.path.dirname(sysconfig_grub_file)):
        os.makedirs(os.path.dirname(sysconfig_grub_file))

    newEntry = grub_conf_entry % stringSubstitutionDict

    debugEntrySubstDict = stringSubstitutionDict.copy()

    debugEntrySubstDict['label'] = "Troubleshooting mode"
    debugEntrySubstDict['bootDirRelativeToGrub'] += "trouble/"
    debugEntrySubstDict['kernelOptions'] += " trouble"
    debugEntry = grub_conf_entry % debugEntrySubstDict
    
    choices = userchoices.getBoot()
    if (os.path.exists(grub_conf_file) and
        (userchoices.getUpgrade() or choices.get('upgrade', False))):
        # For an upgrade, we need to preserve all the settings in the file,
        # not just the titles.  Otherwise, we lose things like password.
        grubFile = open(grub_conf_file)
        grubContents = grubFile.read()
        grubFile.close()
        
        grubContents = removeTitle(grubContents, newEntry.split('\n')[0])
        grubContents = removeTitle(grubContents, debugEntry.split('\n')[0])
        
        grubContents = grubContents.replace('VMware ESX Server',
                                            'VMware ESX Server 3')
        grubContents = grubContents.replace(
            'Service Console only',
            'ESX Server 3 Service Console only')
    else:
        grubContents = grub_conf_header % stringSubstitutionDict

    grubContents = insertTitle(grubContents, debugEntry)
    grubContents = insertTitle(grubContents, newEntry)
    grubContents = changeSetting(grubContents, "default", "0")
    
    if userchoices.getUpgrade():
        grubContents = writeUpgradeFiles(grubContents)
    
    # Write the whole file out first and then use rename to atomically install
    # it in the directory, we don't want to put a broken file in there during
    # an upgrade.
    tmpPath = os.tempnam(os.path.dirname(grub_conf_file), "grub.conf")
    fp = open(tmpPath, 'w')
    fp.write(grubContents)
    fp.close()

    os.chmod(tmpPath, 0600)

    os.rename(tmpPath, grub_conf_file)

    fp = open(device_map_file, 'w')
    fp.write(device_map % stringSubstitutionDict)
    fp.close()

    fp = open(sysconfig_grub_file, 'w')
    fp.write(sysconfig_grub % stringSubstitutionDict)
    fp.close()

#------------------------------------------------------------------------------
def makeInitialRamdisk(stringSubstitutionDict):
    '''Makes the initial ramdisk.'''
    #these 2 should go away soon...
    os.system('touch %s/etc/vmware/sysboot.conf' % instRoot);

    # actually do the mkinitrd
    log.info('Making initrd: ' + cmd_esxcfgboot)
    execCommand("%s -b --update-trouble" % cmd_esxcfgboot, root=instRoot)


#------------------------------------------------------------------------------
def validateGrubPassword(password):
    '''Validates the GRUB Password.
    The GRUB Manual didn't seem to have any specification (section 13.2.10).
    Experimentation shows that the limit is 30 chars
    Raises ValueError if invalid.
    '''
    if not 1 <= len(password) <= 30:
        raise ValueError('GRUB Password length must be between 1 and 30')
    return True
    
#------------------------------------------------------------------------------
class MBRWriter:
    '''Class in charge of writing to the Master Boot Record.'''
    def __enter(self):
        '''This function will ATTEMPT to sync the filesystem
        See the grub manual, section 15.2 for information
        about why this is necessary

        NOTE: Ideally we'd like to do this by unmounting/mounting
        the /boot directory, but if something goes wrong between the
        unmount and the mount, it creates a nightmarish situation
        for diagnosing the bug.  QA would not be happy.
        '''
        # Yes, three times.
        # TODO: talk to some storage guy to see if this is REALLY necessary
        syncKernelBufferToDisk()
        syncKernelBufferToDisk()
        syncKernelBufferToDisk()

    def __exit(self):
        # Yes, three times.
        syncKernelBufferToDisk()
        syncKernelBufferToDisk()
        syncKernelBufferToDisk()
    
    def write(self, stringSubstitutionDict):
        choices = userchoices.getBoot()
        if choices and choices['doNotInstall']:
            log.info('Skipping the writing of the bootloader to disk')
            return

        self.__enter()

        cmd = cmd_grub % stringSubstitutionDict

        rc, stdout, stderr = execCommand(cmd)

        self.checkGrubOutput(stdout)

        self.__exit()

    def checkGrubOutput(self, grubOutString):
        '''Right now this will throw an exception on any GRUB error.
        If we want to get fancy in the future, we can actually parse/lex
        the output from GRUB'''
        for line in grubOutString.splitlines():
            if line.startswith('Error'):
                raise GrubCheckException(grubOutString)
    

#------------------------------------------------------------------------------
def sanityCheck(stringSubstitutionDict, throwOnInsane=False):
    def fail(extraMsg=''):
        log.error('No. ' + extraMsg)
        if throwOnInsane:
            raise Exception('Sanity check failed. '+ extraMsg)

    log.info('Bootloader tasks complete.  Sanity checking the system...')
    log.info('Are the kernel and initrd present in /mnt/sysimage/boot/?')
    bootDir = instRoot +'boot/'
    if os.path.isfile(bootDir+ 'vmlinuz'):
        log.info('Yes.')
    else:
        fail()
    if os.path.isfile(bootDir + 'initrd.img'):
        log.info('Yes.')
    else:
        fail()
    log.info('Is the initrd > 10M?')
    if os.path.getsize(bootDir + 'initrd.img') > 10*(1024*1024):
        log.info('Yes.')
    else:
        fail()

    commented_out_for_now_because_it_causes_PSOD = '''
    log.info('Does the target disk have the GRUB signature?')
    # get the last two bytes of the first 512 bytes on the disk
    cmd = 'dd bs=2 count=1 skip=255 if=%(installerDevDiskName)s |od -x' %\
          stringSubstitutionDict
    rc, stdout, stderr = execCommand(cmd)
    if 'aa55' in stdout:
        log.info('Yes.')
    else:
        fail('Grub signature not found at the tail of the first 512 bytes')
    '''


#------------------------------------------------------------------------------
def _sectorHasGrub(contents):
    # Gleaned from checkbootloader.py
    #   http://aurora.fhive.net/distributions/aurora/build-2.0/sparc/os/
    #     RHupdates/checkbootloader.py

    # XXX I don't like this, but it's what the maintainer suggested :(
    return "GRUB" in contents and contents[-2:] == "\x55\xaa"

#------------------------------------------------------------------------------
def discoverLocation(subDict):
    '''Discover where grub is installed, either on the MBR or the first sector
    of the /boot partition.
    '''
    wholeDiskDev, _partNum = partition.splitPath(subDict['devPartName'])

    diskFile = open(wholeDiskDev)
    mbrContents = diskFile.read(512)
    diskFile.close()

    partFile = open(subDict['devPartName'])
    partBootContents = partFile.read(512)
    partFile.close()

    loc = None
    doNotInstall = False
    if _sectorHasGrub(partBootContents):
        log.debug("found grub installed on the /boot partition")
        loc = userchoices.BOOT_LOC_PARTITION
    elif _sectorHasGrub(mbrContents):
        log.debug("found grub installed on the MBR")
        loc = userchoices.BOOT_LOC_MBR
    else:
        log.warn("grub was not found, not upgrading boot loader")
        doNotInstall = True

    userchoices.setBoot(True, doNotInstall, location=loc)

#------------------------------------------------------------------------------
def runtimeAction():
    pass

#------------------------------------------------------------------------------
def hostAction(context):
    subDict = getStringSubstitutionDict()
    log.debug('Dumping bootloader variables...')
    safeDict = subDict.copy()
    del safeDict['passwordLine']
    log.debug(str(safeDict))

    makeBackups()

    if userchoices.getUpgrade():
        discoverLocation(subDict)
        subDict = getStringSubstitutionDict()

    context.cb.pushStatusGroup(5)
    context.cb.pushStatus('Copying the GRUB images')
    copyGrubStageImages()
    context.cb.popStatus()

    context.cb.pushStatus('Writing the GRUB config files')
    writeGrubConfFiles(subDict)
    context.cb.popStatus()

    context.cb.pushStatus('Making the initial ramdisk')
    makeInitialRamdisk(subDict)
    context.cb.popStatus()

    context.cb.pushStatus('Writing GRUB to the Master Boot Record')
    w = MBRWriter()
    w.write(subDict)
    context.cb.popStatus()
    context.cb.popStatusGroup()

    sanityCheck(subDict)

#------------------------------------------------------------------------------
def hostActionRebuildInitrd(context):
    context.cb.pushStatusGroup(1)
    context.cb.pushStatus('Checking if the initial ramdisk has to be rebuilt')
    # If nothing has changed then this is a noop.
    execCommand("%s --rebuild -b --update-trouble" % cmd_esxcfgboot,
                root=instRoot)
    context.cb.popStatus()
    context.cb.popStatusGroup()
    
if __name__ == '__main__':
    print 'This should probably do some simple test'
