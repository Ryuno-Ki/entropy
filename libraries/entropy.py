#!/usr/bin/python
'''
    # DESCRIPTION:
    # Entropy Object Oriented Interface

    Copyright (C) 2007-2008 Fabio Erculiani

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation; either version 2 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.

    You should have received a copy of the GNU General Public License
    along with this program; if not, write to the Free Software
    Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA
'''

import shutil
import commands
import urllib2
import time
from entropyConstants import *
from outputTools import TextInterface, \
    print_info, print_warning, print_error, \
    red, brown, blue, green, purple, darkgreen, \
    darkred, bold, darkblue, readtext
import exceptionTools
from entropy_i18n import _

try: # try with sqlite3 from python 2.5 - default one
    from sqlite3 import dbapi2
except ImportError: # fallback to embedded pysqlite
    try:
        from pysqlite2 import dbapi2
    except ImportError, e:
        raise exceptionTools.SystemError(
            "%s. %s: %s" % (
                _("Entropy needs a working sqlite+pysqlite or Python compiled with sqlite support"),
                _("Error"),
                e,
            )
        )

class matchContainer:
    def __init__(self):
        self.data = set()

    def inside(self, match):
        if match in self.data:
            return True
        return False

    def add(self, match):
        self.data.add(match)

    def clear(self):
        self.data.clear()

'''
    Main Entropy (client side) package management class
'''
class EquoInterface(TextInterface):

    import dumpTools
    import entropyTools
    def __init__(self, indexing = True, noclientdb = 0, xcache = True, user_xcache = False, repo_validation = True):

        self.dbapi2 = dbapi2 # export for third parties
        self.MaskingParser = None
        self.FileUpdates = None
        self.repoDbCache = {}
        self.securityCache = {}
        self.QACache = {}
        self.spmCache = {}

        self.clientLog = LogFile(level = etpConst['equologlevel'],filename = etpConst['equologfile'], header = "[client]")

        self.urlFetcher = urlFetcher # in this way, can be reimplemented (so you can override updateProgress)
        self.progress = None # supporting external updateProgress stuff, you can point self.progress to your progress bar
                             # and reimplement updateProgress
        self.clientDbconn = None
        self.FtpInterface = FtpInterface # for convenience
        self.indexing = indexing
        self.repo_validation = repo_validation
        self.noclientdb = False
        self.openclientdb = True
        if noclientdb in (False,0):
            self.noclientdb = False
        elif noclientdb in (True,1):
            self.noclientdb = True
        elif noclientdb == 2:
            self.noclientdb = True
            self.openclientdb = False
        self.xcache = xcache
        shell_xcache = os.getenv("ETP_NOCACHE")
        if shell_xcache:
            self.xcache = False

        # now if we are on live, we should disable it
        # are we running on a livecd? (/proc/cmdline has "cdroot")
        if self.entropyTools.islive():
            self.xcache = False
        elif (not self.entropyTools.is_user_in_entropy_group()) and not user_xcache:
            self.xcache = False
        elif not user_xcache:
            self.validate_repositories_cache()

        if not self.xcache and (self.entropyTools.is_user_in_entropy_group()):
            try:
                self.purge_cache(False)
            except:
                pass

        if self.openclientdb:
            self.openClientDatabase()
        self.FileUpdates = self.FileUpdatesInterfaceLoader()

        # masking parser
        self.MaskingParser = self.PackageMaskingParserInterfaceLoader()

        self.validRepositories = []
        if self.repo_validation:
            self.validate_repositories()


    def __del__(self):
        if self.clientDbconn != None:
            del self.clientDbconn
        del self.MaskingParser
        del self.FileUpdates
        self.closeAllRepositoryDatabases()
        self.closeAllSecurity()
        self.closeAllQA()


    def validate_repositories(self):
        # valid repositories
        del self.validRepositories[:]
        for repoid in etpRepositoriesOrder:
            # open database
            try:
                dbc = self.openRepositoryDatabase(repoid)
                dbc.listConfigProtectDirectories()
                dbc.validateDatabase()
                self.validRepositories.append(repoid)
            except exceptionTools.RepositoryError:
                t = _("Repository") + " " + repoid + " " + _("is not available") + ". " + _("Cannot validate")
                self.updateProgress(
                                    darkred(t),
                                    importance = 1,
                                    type = "warning"
                                   )
                continue # repo not available
            except (dbapi2.OperationalError,dbapi2.DatabaseError,exceptionTools.SystemDatabaseError,):
                t = _("Repository") + " " + repoid + " " + _("is corrupted") + ". " + _("Cannot validate")
                self.updateProgress(
                                    darkred(t),
                                    importance = 1,
                                    type = "warning"
                                   )
                continue
        # to avoid having zillions of open files when loading a lot of EquoInterfaces
        self.closeAllRepositoryDatabases(mask_clear = False)

    def setup_default_file_perms(self, filepath):
        # setup file permissions
        os.chmod(filepath,0664)
        if etpConst['entropygid'] != None:
            os.chown(filepath,-1,etpConst['entropygid'])

    def _resources_run_create_lock(self):
        self.create_pid_file_lock(etpConst['locks']['using_resources'])

    def _resources_run_remove_lock(self):
        if os.path.isfile(etpConst['locks']['using_resources']):
            os.remove(etpConst['locks']['using_resources'])

    def _resources_run_check_lock(self):
        rc = self.check_pid_file_lock(etpConst['locks']['using_resources'])
        return rc

    def check_pid_file_lock(self, pidfile):
        if not os.path.isfile(pidfile):
            return False # not locked
        f = open(pidfile)
        s_pid = f.readline().strip()
        f.close()
        try:
            s_pid = int(s_pid)
        except ValueError:
            return False # not locked
        # is it our pid?
        mypid = os.getpid()
        if (s_pid != mypid) and os.path.isdir("%s/proc/%s" % (etpConst['systemroot'],s_pid,)):
            # is it running
            return True # locked
        return False

    def create_pid_file_lock(self, pidfile, mypid = None):
        lockdir = os.path.dirname(pidfile)
        if not os.path.isdir(lockdir):
            os.makedirs(lockdir,0775)
        const_setup_perms(lockdir,etpConst['entropygid'])
        if mypid == None:
            mypid = os.getpid()
        f = open(pidfile,"w")
        f.write(str(mypid))
        f.flush()
        f.close()

    def application_lock_check(self, silent = False):
        # check if another instance is running
        etpConst['applicationlock'] = False
        const_setupEntropyPid()
        locked = self.entropyTools.applicationLockCheck(option = None, gentle = True, silent = True)
        if locked:
            if not silent:
                self.updateProgress(
                    red(_("Another Entropy instance is currently active, cannot satisfy your request.")),
                    importance = 1,
                    type = "error",
                    header = darkred(" @@ ")
                )
            return True
        return False

    def lock_check(self, check_function):

        lock_count = 0
        max_lock_count = 600
        sleep_seconds = 0.5

        # check lock file
        while 1:
            locked = check_function()
            if not locked:
                if lock_count > 0:
                    self.updateProgress(
                        blue(_("Resources unlocked, let's go!")),
                        importance = 1,
                        type = "info",
                        header = darkred(" @@ ")
                    )
                break
            if lock_count >= max_lock_count:
                mycalc = max_lock_count*sleep_seconds/60
                self.updateProgress(
                    blue(_("Resources still locked after %s minutes, giving up!")) % (mycalc,),
                    importance = 1,
                    type = "warning",
                    header = darkred(" @@ ")
                )
                return True # gave up
            lock_count += 1
            self.updateProgress(
                blue(_("Resources locked, sleeping %s seconds, check #%s/%s")) % (
                        sleep_seconds,
                        lock_count,
                        max_lock_count,
                ),
                importance = 1,
                type = "warning",
                header = darkred(" @@ "),
                back = True
            )
            time.sleep(sleep_seconds)
        return False # yay!

    def validate_repositories_cache(self):
        # is the list of repos changed?
        cached = self.dumpTools.loadobj(etpCache['repolist'])
        if cached == None:
            # invalidate matching cache
            try:
                self.repository_move_clear_cache()
            except IOError:
                pass
        elif type(cached) is tuple:
            # compare
            myrepolist = tuple(etpRepositoriesOrder)
            if cached != myrepolist:
                cached = set(cached)
                myrepolist = set(myrepolist)
                difflist = cached - myrepolist # before minus now
                for repoid in difflist:
                    try:
                        self.repository_move_clear_cache(repoid)
                    except IOError:
                        pass
        try:
            self.dumpTools.dumpobj(etpCache['repolist'],tuple(etpRepositoriesOrder))
        except IOError:
            pass

    def backup_setting(self, setting_name):
        if etpConst.has_key(setting_name):
            myinst = etpConst[setting_name]
            if type(etpConst[setting_name]) in (list,tuple):
                myinst = etpConst[setting_name][:]
            elif type(etpConst[setting_name]) in (dict,set):
                myinst = etpConst[setting_name].copy()
            else:
                myinst = etpConst[setting_name]
            etpConst['backed_up'].update({setting_name: myinst})
        else:
            t = _("Nothing to backup in etpConst with %s key") % (setting_name,)
            raise exceptionTools.InvalidData("InvalidData: %s" % (t,))

    def set_priority(self, low = 0):
        default_nice = etpConst['default_nice']
        current_nice = etpConst['current_nice']
        delta = current_nice - default_nice
        try:
            etpConst['current_nice'] = os.nice(delta*-1+low)
        except OSError:
            pass
        return current_nice # aka, the old value

    def switchChroot(self, chroot = ""):
        # clean caches
        self.purge_cache()
        const_resetCache()
        self.closeAllRepositoryDatabases()
        if chroot.endswith("/"):
            chroot = chroot[:-1]
        etpSys['rootdir'] = chroot
        initConfig_entropyConstants(etpSys['rootdir'])
        initConfig_clientConstants()
        self.validate_repositories()
        self.reopenClientDbconn()
        if chroot:
            try:
                self.clientDbconn.resetTreeupdatesDigests()
            except:
                pass
        # I don't think it's safe to keep them open
        # isn't it?
        self.closeAllSecurity()
        self.closeAllQA()

    def Security(self):
        chroot = etpConst['systemroot']
        cached = self.securityCache.get(chroot)
        if cached != None:
            return cached
        cached = SecurityInterface(self)
        self.securityCache[chroot] = cached
        return cached

    def QA(self):
        chroot = etpConst['systemroot']
        cached = self.QACache.get(chroot)
        if cached != None:
            return cached
        cached = QAInterface(self)
        self.QACache[chroot] = cached
        return cached

    def closeAllQA(self):
        self.QACache.clear()

    def closeAllSecurity(self):
        self.securityCache.clear()

    def reopenClientDbconn(self):
        self.clientDbconn.closeDB()
        self.openClientDatabase()

    def closeAllRepositoryDatabases(self, mask_clear = True):
        for item in self.repoDbCache:
            self.repoDbCache[item].closeDB()
        self.repoDbCache.clear()
        if mask_clear:
            etpConst['packagemasking'] = None

    def openClientDatabase(self):
        if not os.path.isdir(os.path.dirname(etpConst['etpdatabaseclientfilepath'])):
            os.makedirs(os.path.dirname(etpConst['etpdatabaseclientfilepath']))
        if (not self.noclientdb) and (not os.path.isfile(etpConst['etpdatabaseclientfilepath'])):
            t = _("System database not found or corrupted")
            raise exceptionTools.SystemDatabaseError("SystemDatabaseError: %s: %s" % (
                    t,
                    etpConst['etpdatabaseclientfilepath'],
                )
            )
        conn = EntropyDatabaseInterface(
            readOnly = False,
            dbFile = etpConst['etpdatabaseclientfilepath'],
            clientDatabase = True,
            dbname = etpConst['clientdbid'],
            xcache = self.xcache,
            indexing = self.indexing,
            OutputInterface = self
        )
        # validate database
        if not self.noclientdb:
            conn.validateDatabase()
        if not etpConst['dbconfigprotect']:

            if conn.doesTableExist('configprotect') and conn.doesTableExist('configprotectreference'):
                etpConst['dbconfigprotect'] = conn.listConfigProtectDirectories()
            if conn.doesTableExist('configprotectmask') and conn.doesTableExist('configprotectreference'):
                etpConst['dbconfigprotectmask'] = conn.listConfigProtectDirectories(mask = True)

            etpConst['dbconfigprotect'] = [etpConst['systemroot']+x for x in etpConst['dbconfigprotect']]
            etpConst['dbconfigprotectmask'] = [etpConst['systemroot']+x for x in etpConst['dbconfigprotect']]

            etpConst['dbconfigprotect'] += [etpConst['systemroot']+x for x in etpConst['configprotect'] if etpConst['systemroot']+x not in etpConst['dbconfigprotect']]
            etpConst['dbconfigprotectmask'] += [etpConst['systemroot']+x for x in etpConst['configprotectmask'] if etpConst['systemroot']+x not in etpConst['dbconfigprotectmask']]

        self.clientDbconn = conn
        return self.clientDbconn

    def clientDatabaseSanityCheck(self):
        self.updateProgress(
            darkred(_("Sanity Check") + ": " + _("system database")),
            importance = 2,
            type = "warning"
        )
        idpkgs = self.clientDbconn.listAllIdpackages()
        length = len(idpkgs)
        count = 0
        errors = False
        scanning_txt = _("Scanning...")
        for x in idpkgs:
            count += 1
            self.updateProgress(
                                    darkgreen(scanning_txt),
                                    importance = 0,
                                    type = "info",
                                    back = True,
                                    count = (count,length),
                                    percent = True
                                )
            try:
                self.clientDbconn.getPackageData(x)
            except Exception ,e:
                self.entropyTools.printTraceback()
                errors = True
                self.updateProgress(
                    darkred(_("Errors on idpackage %s, error: %s")) % (x,str(e)),
                    importance = 0,
                    type = "warning"
                )

        if not errors:
            t = _("Sanity Check") + ": %s" % (bold(_("PASSED")),)
            self.updateProgress(
                darkred(t),
                importance = 2,
                type = "warning"
            )
            return 0
        else:
            t = _("Sanity Check") + ": %s" % (bold(_("CORRUPTED")),)
            self.updateProgress(
                darkred(t),
                importance = 2,
                type = "warning"
            )
            return -1

    def openRepositoryDatabase(self, repoid):
        if not self.repoDbCache.has_key((repoid,etpConst['systemroot'])) or (etpConst['packagemasking'] == None):
            if etpConst['packagemasking'] == None:
                self.closeAllRepositoryDatabases()
            dbconn = self.loadRepositoryDatabase(repoid, xcache = self.xcache, indexing = self.indexing)
            try:
                dbconn.checkDatabaseApi()
            except:
                pass
            self.repoDbCache[(repoid,etpConst['systemroot'])] = dbconn
            return dbconn
        else:
            return self.repoDbCache.get((repoid,etpConst['systemroot']))

    def parse_masking_settings(self):
        etpConst['packagemasking'] = self.MaskingParser.parse()
        # merge universal keywords
        for x in etpConst['packagemasking']['keywords']['universal']:
            etpConst['keywords'].add(x)

    '''
    @description: open the repository database
    @input repositoryName: name of the client database
    @input xcache: loads on-disk cache
    @input indexing: indexes SQL tables
    @output: database class instance
    NOTE: DO NOT USE THIS DIRECTLY, BUT USE EquoInterface.openRepositoryDatabase
    '''
    def loadRepositoryDatabase(self, repositoryName, xcache = True, indexing = True):

        # load the masking parser
        if etpConst['packagemasking'] == None:
            self.parse_masking_settings()
        if repositoryName.endswith(etpConst['packagesext']):
            xcache = False

        dbfile = etpRepositories[repositoryName]['dbpath']+"/"+etpConst['etpdatabasefile']
        if not os.path.isfile(dbfile):
            t = _("Repository %s hasn't been downloaded yet.") % (repositoryName,)
            if repositoryName not in repo_error_messages_cache:
                self.updateProgress(
                    darkred(t),
                    importance = 2,
                    type = "warning"
                )
                repo_error_messages_cache.add(repositoryName)
            raise exceptionTools.RepositoryError("RepositoryError: %s" % (t,))

        conn = EntropyDatabaseInterface(
            readOnly = True,
            dbFile = dbfile,
            clientDatabase = True,
            dbname = etpConst['dbnamerepoprefix']+repositoryName,
            xcache = xcache,
            indexing = indexing,
            OutputInterface = self
        )
        # initialize CONFIG_PROTECT
        if (etpRepositories[repositoryName]['configprotect'] == None) or \
            (etpRepositories[repositoryName]['configprotectmask'] == None):

            try:
                etpRepositories[repositoryName]['configprotect'] = conn.listConfigProtectDirectories()
            except (dbapi2.OperationalError, dbapi2.DatabaseError):
                etpRepositories[repositoryName]['configprotect'] = []
            try:
                etpRepositories[repositoryName]['configprotectmask'] = conn.listConfigProtectDirectories(mask = True)
            except (dbapi2.OperationalError, dbapi2.DatabaseError):
                etpRepositories[repositoryName]['configprotectmask'] = []

            etpRepositories[repositoryName]['configprotect'] = [etpConst['systemroot']+x for x in etpRepositories[repositoryName]['configprotect']]
            etpRepositories[repositoryName]['configprotectmask'] = [etpConst['systemroot']+x for x in etpRepositories[repositoryName]['configprotectmask']]

            etpRepositories[repositoryName]['configprotect'] += [etpConst['systemroot']+x for x in etpConst['configprotect'] if etpConst['systemroot']+x not in etpRepositories[repositoryName]['configprotect']]
            etpRepositories[repositoryName]['configprotectmask'] += [etpConst['systemroot']+x for x in etpConst['configprotectmask'] if etpConst['systemroot']+x not in etpRepositories[repositoryName]['configprotectmask']]
        if (repositoryName not in etpConst['client_treeupdatescalled']) and (self.entropyTools.is_user_in_entropy_group()) and (not repositoryName.endswith(etpConst['packagesext'])):
            try:
                conn.clientUpdatePackagesData(self.clientDbconn)
            except (dbapi2.OperationalError, dbapi2.DatabaseError):
                pass
        return conn

    def openGenericDatabase(self, dbfile, dbname = None, xcache = None, readOnly = False, indexing_override = None):
        if xcache == None:
            xcache = self.xcache
        if indexing_override != None:
            indexing = indexing_override
        else:
            indexing = self.indexing
        if dbname == None:
            dbname = etpConst['genericdbid']
        return EntropyDatabaseInterface(
            readOnly = readOnly,
            dbFile = dbfile,
            clientDatabase = True,
            dbname = dbname,
            xcache = xcache,
            indexing = indexing,
            OutputInterface = self
        )

    def listAllAvailableBranches(self):
        branches = set()
        for repo in self.validRepositories:
            dbconn = self.openRepositoryDatabase(repo)
            branches.update(dbconn.listAllBranches())
        return branches


    '''
       Cache stuff :: begin
    '''
    def purge_cache(self, showProgress = True, client_purge = True):
        const_resetCache()
        if self.entropyTools.is_user_in_entropy_group():
            skip = set()
            if not client_purge:
                skip.add("/"+etpCache['dbMatch']+"/"+etpConst['clientdbid']) # it's ok this way
                skip.add("/"+etpCache['dbSearch']+"/"+etpConst['clientdbid']) # it's ok this way
            for key in etpCache:
                if showProgress:
                    self.updateProgress(
                        darkred(_("Cleaning %s => *.dmp...")) % (etpCache[key],),
                        importance = 1,
                        type = "warning",
                        back = True
                    )
                self.clear_dump_cache(etpCache[key], skip = skip)

            if showProgress:
                self.updateProgress(
                    darkgreen(_("Cache is now empty.")),
                    importance = 2,
                    type = "info"
                )

    def generate_cache(self, depcache = True, configcache = True, client_purge = True, install_queue = True):
        # clean first of all
        self.purge_cache(client_purge = client_purge)
        if depcache:
            self.do_depcache(do_install_queue = install_queue)
        if configcache:
            self.do_configcache()

    def do_configcache(self):
        self.updateProgress(
            darkred(_("Configuration files")),
            importance = 2,
            type = "warning"
        )
        self.updateProgress(
            red(_("Scanning hard disk")),
            importance = 1,
            type = "warning"
        )
        self.FileUpdates.scanfs(dcache = False)
        self.updateProgress(
            darkred(_("Cache generation complete.")),
            importance = 2,
            type = "info"
        )

    def do_depcache(self, do_install_queue = True):

        self.updateProgress(
            darkgreen(_("Resolving metadata")),
            importance = 1,
            type = "warning"
        )
        # we can barely ignore any exception from here
        # especially cases where client db does not exist
        try:
            update, remove, fine = self.calculate_world_updates()
            del fine, remove
            if do_install_queue:
                self.retrieveInstallQueue(update, False, False)
            self.calculate_available_packages()
            # otherwise world cache will be trashed at the next initialization
            self.dumpTools.dumpobj(etpCache['repolist'],tuple(etpRepositoriesOrder))
        except:
            pass

        self.updateProgress(
            darkred(_("Dependencies cache filled.")),
            importance = 2,
            type = "warning"
        )

    def clear_dump_cache(self, dump_name, skip = []):
        dump_path = os.path.join(etpConst['dumpstoragedir'],dump_name)
        dump_dir = os.path.dirname(dump_path)
        #dump_file = os.path.basename(dump_path)
        for currentdir, subdirs, files in os.walk(dump_dir):
            path = os.path.join(dump_dir,currentdir)
            if skip:
                found = False
                for myskip in skip:
                    if path.find(myskip) != -1:
                        found = True
                        break
                if found: continue
            for item in files:
                if item.endswith(".dmp"):
                    item = os.path.join(path,item)
                    os.remove(item)
            if not os.listdir(path):
                os.rmdir(path)

    '''
       Cache stuff :: end
    '''

    def dependencies_test(self, dbconn = None):

        if dbconn == None:
            dbconn = self.clientDbconn
        # get all the installed packages
        installedPackages = dbconn.listAllIdpackages()

        deps_not_matched = set()
        # now look
        length = str((len(installedPackages)))
        count = 0
        for xidpackage in installedPackages:
            count += 1
            atom = dbconn.retrieveAtom(xidpackage)
            self.updateProgress(
                                    darkgreen(_("Checking %s") % (bold(atom),)),
                                    importance = 0,
                                    type = "info",
                                    back = True,
                                    count = (count,length),
                                    header = darkred(" @@ ")
                                )

            xdeps = dbconn.retrieveDependencies(xidpackage)
            needed_deps = set()
            for xdep in xdeps:
                xmatch = dbconn.atomMatch(xdep)
                if xmatch[0] == -1:
                    needed_deps.add(xdep)

            deps_not_matched |= needed_deps

        return deps_not_matched

    def find_belonging_dependency(self, matched_atoms):
        crying_atoms = set()
        for atom in matched_atoms:
            for repo in self.validRepositories:
                rdbconn = self.openRepositoryDatabase(repo)
                riddep = rdbconn.searchDependency(atom)
                if riddep != -1:
                    ridpackages = rdbconn.searchIdpackageFromIddependency(riddep)
                    for i in ridpackages:
                        i,r = rdbconn.idpackageValidator(i)
                        if i == -1:
                            continue
                        iatom = rdbconn.retrieveAtom(i)
                        crying_atoms.add((iatom,repo))
        return crying_atoms

    def get_licenses_to_accept(self, install_queue):
        if not install_queue:
            return {}
        licenses = {}
        for match in install_queue:
            repoid = match[1]
            dbconn = self.openRepositoryDatabase(repoid)
            wl = etpConst['packagemasking']['repos_license_whitelist'].get(repoid)
            if not wl:
                continue
            keys = dbconn.retrieveLicensedataKeys(match[0])
            for key in keys:
                if key not in wl:
                    found = self.clientDbconn.isLicenseAccepted(key)
                    if found:
                        continue
                    if not licenses.has_key(key):
                        licenses[key] = set()
                    licenses[key].add(match)
        return licenses

    def get_text_license(self, license_name, repoid):
        dbconn = self.openRepositoryDatabase(repoid)
        text = dbconn.retrieveLicenseText(license_name)
        tempfile = self.entropyTools.getRandomTempFile()
        f = open(tempfile,"w")
        f.write(text)
        f.flush()
        f.close()
        return tempfile

    def get_file_viewer(self):
        viewer = None
        if os.access("/usr/bin/less",os.X_OK):
            viewer = "/usr/bin/less"
        elif os.access("/bin/more",os.X_OK):
            viewer = "/bin/more"
        if not viewer:
            viewer = self.get_file_editor()
        return viewer

    def get_file_editor(self):
        editor = None
        if os.getenv("EDITOR"):
            editor = "$EDITOR"
        elif os.access("/bin/nano",os.X_OK):
            editor = "/bin/nano"
        elif os.access("/bin/vi",os.X_OK):
            editor = "/bin/vi"
        elif os.access("/usr/bin/vi",os.X_OK):
            editor = "/usr/bin/vi"
        elif os.access("/usr/bin/emacs",os.X_OK):
            editor = "/usr/bin/emacs"
        elif os.access("/bin/emacs",os.X_OK):
            editor = "/bin/emacs"
        return editor

    def libraries_test(self, dbconn = None):

        if dbconn == None:
            dbconn = self.clientDbconn

        self.updateProgress(
            blue(_("Libraries test")),
            importance = 2,
            type = "info",
            header = red(" @@ ")
        )

        if not etpConst['systemroot']:
            myroot = "/"
        else:
            myroot = etpConst['systemroot']+"/"
        # run ldconfig first
        os.system("ldconfig -r "+myroot+" &> /dev/null")
        # open /etc/ld.so.conf
        if not os.path.isfile(etpConst['systemroot']+"/etc/ld.so.conf"):
            self.updateProgress(
                blue(_("Cannot find "))+red(etpConst['systemroot']+"/etc/ld.so.conf"),
                importance = 1,
                type = "error",
                header = red(" @@ ")
            )
            return set(),set(),-1

        ldpaths = set(self.entropyTools.collectLinkerPaths())
        ldpaths |= self.entropyTools.collectPaths()
        # speed up when /usr/lib is a /usr/lib64 symlink
        if "/usr/lib64" in ldpaths and "/usr/lib" in ldpaths:
            if os.path.realpath("/usr/lib64") == "/usr/lib":
                ldpaths.remove("/usr/lib")

        executables = set()
        total = len(ldpaths)
        count = 0
        for ldpath in ldpaths:
            count += 1
            self.updateProgress(
                blue("Tree: ")+red(etpConst['systemroot']+ldpath),
                importance = 0,
                type = "info",
                count = (count,total),
                back = True,
                percent = True,
                header = "  "
            )
            ldpath = ldpath.encode(sys.getfilesystemencoding())
            for currentdir,subdirs,files in os.walk(etpConst['systemroot']+ldpath):
                for item in files:
                    filepath = os.path.join(currentdir,item)
                    if filepath in etpConst['libtest_files_blacklist']:
                        continue
                    if not os.access(filepath,os.X_OK):
                        continue
                    if not self.entropyTools.is_elf_file(filepath):
                        continue
                    executables.add(filepath[len(etpConst['systemroot']):])

        self.updateProgress(
            blue(_("Collecting broken executables")),
            importance = 2,
            type = "info",
            header = red(" @@ ")
        )
        t = red(_("Attention")) + ": " + \
            blue(_("don't worry about libraries that are shown here but not later."))
        self.updateProgress(
            t,
            importance = 1,
            type = "info",
            header = red(" @@ ")
        )

        myQA = self.QA()

        plain_brokenexecs = set()
        total = len(executables)
        count = 0
        scan_txt = blue("%s ..." % (_("Scanning libraries"),))
        for executable in executables:
            count += 1
            if (count%10 == 0) or (count == total) or (count == 1):
                self.updateProgress(
                    scan_txt,
                    importance = 0,
                    type = "info",
                    count = (count,total),
                    back = True,
                    percent = True,
                    header = "  "
                )
            myelfs = self.entropyTools.read_elf_dynamic_libraries(etpConst['systemroot']+executable)
            mylibs = set()
            for mylib in myelfs:
                found = myQA.resolve_dynamic_library(mylib, executable)
                if found:
                    continue
                mylibs.add(mylib)
            if not mylibs:
                continue

            alllibs = blue(' :: ').join(list(mylibs))
            self.updateProgress(
                red(etpConst['systemroot']+executable)+" [ "+alllibs+" ]",
                importance = 1,
                type = "info",
                percent = True,
                count = (count,total),
                header = "  "
            )
            plain_brokenexecs.add(executable)

        del executables
        packagesMatched = {}

        if not etpSys['serverside']:

            self.updateProgress(
                blue(_("Matching broken libraries/executables")),
                importance = 1,
                type = "info",
                header = red(" @@ ")
            )
            matched = set()
            for brokenlib in plain_brokenexecs:
                idpackages = self.clientDbconn.searchBelongs(brokenlib)
                for idpackage in idpackages:
                    key, slot = self.clientDbconn.retrieveKeySlot(idpackage)
                    mymatch = self.atomMatch(key, matchSlot = slot)
                    if mymatch[0] == -1:
                        matched.add(brokenlib)
                        continue
                    cmpstat = self.get_package_action(mymatch)
                    if cmpstat == 0:
                        continue
                    if not packagesMatched.has_key(brokenlib):
                        packagesMatched[brokenlib] = set()
                    packagesMatched[brokenlib].add(mymatch)
                    matched.add(brokenlib)
            plain_brokenexecs -= matched

        return packagesMatched,plain_brokenexecs,0

    def move_to_branch(self, branch, pretend = False):
        availbranches = self.listAllAvailableBranches()
        if branch not in availbranches:
            return 1
        if pretend:
            return 0
        if branch != etpConst['branch']:
            etpConst['branch'] = branch
            # update configuration
            self.entropyTools.writeNewBranch(branch)
            # reset treeupdatesactions
            self.clientDbconn.resetTreeupdatesDigests()
            # clean cache
            self.purge_cache(showProgress = False)
            # reopen Client Database, this will make treeupdates to be re-read
            self.reopenClientDbconn()
            self.closeAllRepositoryDatabases()
            self.validate_repositories()
            initConfig_entropyConstants(etpSys['rootdir'])
        return 0

    # tell if a new equo release is available, returns True or False
    def check_equo_updates(self):
        found, match = self.check_package_update("app-admin/equo", deep = True)
        return found

    '''
        @input: matched atom (idpackage,repoid)
        @output:
                upgrade: int(2)
                install: int(1)
                reinstall: int(0)
                downgrade: int(-1)
    '''
    def get_package_action(self, match):
        dbconn = self.openRepositoryDatabase(match[1])
        pkgkey, pkgslot = dbconn.retrieveKeySlot(match[0])
        results = self.clientDbconn.searchKeySlot(pkgkey, pkgslot)
        if not results:
            return 1

        installed_idpackage = results[0][0]
        pkgver = dbconn.retrieveVersion(match[0])
        pkgtag = dbconn.retrieveVersionTag(match[0])
        pkgrev = dbconn.retrieveRevision(match[0])
        installedVer = self.clientDbconn.retrieveVersion(installed_idpackage)
        installedTag = self.clientDbconn.retrieveVersionTag(installed_idpackage)
        installedRev = self.clientDbconn.retrieveRevision(installed_idpackage)
        pkgcmp = self.entropyTools.entropyCompareVersions((pkgver,pkgtag,pkgrev),(installedVer,installedTag,installedRev))
        if pkgcmp == 0:
            return 0
        elif pkgcmp > 0:
            return 2
        else:
            return -1

    def get_meant_packages(self, search_term):
        import re
        match_string = ''
        for x in search_term:
            if x.isalpha():
                x = "(%s{1,})?" % (x,)
                match_string += x
        match_exp = re.compile(match_string,re.IGNORECASE)

        matched = {}
        for repo in self.validRepositories:
            dbconn = self.openRepositoryDatabase(repo)
            # get names
            idpackages = dbconn.listAllIdpackages(branch = etpConst['branch'], branch_operator = "<=")
            for idpackage in idpackages:
                name = dbconn.retrieveName(idpackage)
                if len(name) < len(search_term):
                    continue
                mymatches = match_exp.findall(name)
                found_matches = []
                for mymatch in mymatches:
                    items = len([x for x in mymatch if x != ""])
                    if items < 1:
                        continue
                    calc = float(items)/len(mymatch)
                    if calc > 0.0:
                        found_matches.append(calc)
                if not found_matches:
                    continue
                maxpoint = max(found_matches)
                if not matched.has_key(maxpoint):
                    matched[maxpoint] = set()
                matched[maxpoint].add((idpackage,repo))
        if matched:
            mydata = []
            while len(mydata) < 5:
                try:
                    most = max(matched.keys())
                except ValueError:
                    break
                popped = matched.pop(most)
                mydata.extend(list(popped))
            return mydata
        return set()

    # better to use key:slot
    def check_package_update(self, atom, deep = False):

        if self.xcache:
            c_hash = str(hash(atom))+str(hash(deep))
            c_hash = str(hash(c_hash))
            cached = self.dumpTools.loadobj(etpCache['check_package_update']+c_hash)
            if cached != None:
                return cached

        found = False
        match = self.clientDbconn.atomMatch(atom)
        matched = None
        if match[0] != -1:
            myatom = self.clientDbconn.retrieveAtom(match[0])
            mytag = self.entropyTools.dep_gettag(myatom)
            myatom = self.entropyTools.remove_tag(myatom)
            myrev = self.clientDbconn.retrieveRevision(match[0])
            pkg_match = "="+myatom+"~"+str(myrev)
            if mytag != None:
                pkg_match += "#%s" % (mytag,)
            pkg_unsatisfied,x = self.filterSatisfiedDependencies([pkg_match], deep_deps = deep)
            del x
            if pkg_unsatisfied:
                found = True
            del pkg_unsatisfied
            matched = self.atomMatch(pkg_match)
        del match

        if self.xcache:
            try:
                self.dumpTools.dumpobj(etpCache['check_package_update']+c_hash,(found,matched))
            except:
                pass

        return found, matched


    # @returns -1 if the file does not exist or contains bad data
    # @returns int>0 if the file exists
    def get_repository_revision(self, reponame):
        if os.path.isfile(etpRepositories[reponame]['dbpath']+"/"+etpConst['etpdatabaserevisionfile']):
            f = open(etpRepositories[reponame]['dbpath']+"/"+etpConst['etpdatabaserevisionfile'],"r")
            try:
                revision = int(f.readline().strip())
            except:
                revision = -1
            f.close()
        else:
            revision = -1
        return revision

    def update_repository_revision(self, reponame):
        r = self.get_repository_revision(reponame)
        etpRepositories[reponame]['dbrevision'] = "0"
        if r != -1:
            etpRepositories[reponame]['dbrevision'] = str(r)

    # @returns -1 if the file does not exist
    # @returns int>0 if the file exists
    def get_repository_db_file_checksum(self, reponame):
        if os.path.isfile(etpRepositories[reponame]['dbpath']+"/"+etpConst['etpdatabasehashfile']):
            f = open(etpRepositories[reponame]['dbpath']+"/"+etpConst['etpdatabasehashfile'],"r")
            try:
                mhash = f.readline().strip().split()[0]
            except:
                mhash = "-1"
            f.close()
        else:
            mhash = "-1"
        return mhash


    def fetch_repository_if_not_available(self, reponame):
        if fetch_repository_if_not_available_cache.has_key(reponame):
            return fetch_repository_if_not_available_cache.get(reponame)
        # open database
        rc = 0
        dbfile = etpRepositories[reponame]['dbpath']+"/"+etpConst['etpdatabasefile']
        if not os.path.isfile(dbfile):
            # sync
            repoConn = self.Repositories(reponames = [reponame], noEquoCheck = True)
            rc = repoConn.sync()
            del repoConn
            if os.path.isfile(dbfile):
                rc = 0
        fetch_repository_if_not_available_cache[reponame] = rc
        return rc

    def atomMatch(          self,
                            atom,
                            caseSensitive = True,
                            matchSlot = None,
                            matchBranches = (),
                            packagesFilter = True,
                            multiMatch = False,
                            multiRepo = False,
                            matchRevision = None,
                            matchRepo = None,
                            server_repos = [],
                            serverInstance = None,
                            extendedResults = False
                                                        ):

        if not server_repos:
            # support match in repository from shell
            # atom@repo1,repo2,repo3
            atom, repos = self.entropyTools.dep_get_match_in_repos(atom)
            if (matchRepo == None) and (repos != None):
                matchRepo = repos

        if self.xcache:

            if matchRepo and (type(matchRepo) in (list,tuple,set)):
                u_hash = hash(tuple(matchRepo))
            else:
                u_hash = hash(matchRepo)
            c_hash =    str(hash(atom)) + \
                        str(hash(matchSlot)) + \
                        str(hash(tuple(matchBranches))) + \
                        str(hash(packagesFilter)) + \
                        str(hash(tuple(self.validRepositories))) + \
                        str(hash(tuple(etpRepositories.keys()))) + \
                        str(hash(multiMatch)) + \
                        str(hash(multiRepo)) + \
                        str(hash(caseSensitive)) + \
                        str(hash(matchRevision)) + \
                        str(hash(extendedResults))+ \
                        str(u_hash)
            c_hash = str(hash(c_hash))
            cached = self.dumpTools.loadobj(etpCache['atomMatch']+c_hash)
            if cached != None:
                return cached

        if server_repos:
            if not serverInstance:
                t = _("server_repos needs serverInstance")
                raise exceptionTools.IncorrectParameter("IncorrectParameter: %s" % (t,))
            valid_repos = server_repos[:]
        else:
            valid_repos = self.validRepositories
            if matchRepo and (type(matchRepo) in (list,tuple,set)):
                valid_repos = list(matchRepo)

        def open_db(repoid):
            if server_repos:
                dbconn = serverInstance.openServerDatabase(just_reading = True, repo = repoid)
            else:
                dbconn = self.openRepositoryDatabase(repoid)
            return dbconn

        repoResults = {}
        for repo in valid_repos:

            # check if repo exists
            if not repo.endswith(etpConst['packagesext']) and not server_repos:
                fetch = self.fetch_repository_if_not_available(repo)
                if fetch != 0:
                    continue # cannot fetch repo, excluding

            # search
            dbconn = open_db(repo)
            query = dbconn.atomMatch(   atom,
                                        caseSensitive = caseSensitive,
                                        matchSlot = matchSlot,
                                        matchBranches = matchBranches,
                                        packagesFilter = packagesFilter,
                                        matchRevision = matchRevision,
                                        extendedResults = extendedResults
            )
            if query[1] == 0:
                # package found, add to our dictionary
                if extendedResults:
                    repoResults[repo] = (query[0][0],query[0][2],query[0][3],query[0][4])
                else:
                    repoResults[repo] = query[0]

        if extendedResults:
            dbpkginfo = ((-1,None,None,None),1)
        else:
            dbpkginfo = (-1,1)

        packageInformation = {}

        if multiRepo and repoResults:
            data = set()
            for repoid in repoResults:
                data.add((repoResults[repoid],repoid))
            dbpkginfo = (data,0)

        elif len(repoResults) == 1:
            # one result found
            repo = repoResults.keys()[0]
            dbpkginfo = (repoResults[repo],repo)

        elif len(repoResults) > 1:
            # we have to decide which version should be taken

            # .tbz2 repos have always the precedence, so if we find them,
            # we should second what user wants, installing his tbz2
            tbz2repos = [x for x in repoResults if x.endswith(etpConst['packagesext'])]
            if tbz2repos:
                del tbz2repos
                newrepos = repoResults.copy()
                for x in newrepos:
                    if not x.endswith(etpConst['packagesext']):
                        del repoResults[x]

            version_duplicates = set()
            versions = []
            for repo in repoResults:
                packageInformation[repo] = {}
                if extendedResults:
                    version = repoResults[repo][1]
                    packageInformation[repo]['versiontag'] = repoResults[repo][2]
                    packageInformation[repo]['revision'] = repoResults[repo][3]
                    packageInformation[repo]['version'] = version
                else:
                    dbconn = open_db(repo)
                    packageInformation[repo]['versiontag'] = dbconn.retrieveVersionTag(repoResults[repo])
                    packageInformation[repo]['revision'] = dbconn.retrieveRevision(repoResults[repo])
                    version = dbconn.retrieveVersion(repoResults[repo])
                packageInformation[repo]['version'] = version
                if version in versions:
                    version_duplicates.add(version)
                versions.append(version)


            newerVersion = self.entropyTools.getNewerVersion(versions)[0]
            # if no duplicates are found, we're done
            if not version_duplicates:

                for reponame in packageInformation:
                    if packageInformation[reponame]['version'] == newerVersion:
                        break
                dbpkginfo = (repoResults[reponame],reponame)

            else:

                if newerVersion not in version_duplicates:

                    # we are fine, the newerVersion is not one of the duplicated ones
                    for reponame in packageInformation:
                        if packageInformation[reponame]['version'] == newerVersion:
                            break
                    dbpkginfo = (repoResults[reponame],reponame)

                else:

                    del version_duplicates
                    conflictingEntries = {}
                    tags_duplicates = set()
                    tags = []
                    for repo in packageInformation:
                        if packageInformation[repo]['version'] == newerVersion:
                            conflictingEntries[repo] = {}
                            versiontag = packageInformation[repo]['versiontag']
                            if versiontag in tags:
                                tags_duplicates.add(versiontag)
                            tags.append(versiontag)
                            conflictingEntries[repo]['versiontag'] = versiontag
                            conflictingEntries[repo]['revision'] = packageInformation[repo]['revision']

                    del packageInformation
                    newerTag = tags[:]
                    newerTag.reverse()
                    newerTag = newerTag[0]
                    if not newerTag in tags_duplicates:

                        # we're finally done
                        for reponame in conflictingEntries:
                            if conflictingEntries[reponame]['versiontag'] == newerTag:
                                break
                        dbpkginfo = (repoResults[reponame],reponame)

                    else:

                        # yes, it is. we need to compare revisions
                        conflictingRevisions = {}
                        revisions = []
                        revisions_duplicates = set()
                        for repo in conflictingEntries:
                            if conflictingEntries[repo]['versiontag'] == newerTag:
                                conflictingRevisions[repo] = {}
                                versionrev = conflictingEntries[repo]['revision']
                                if versionrev in revisions:
                                    revisions_duplicates.add(versionrev)
                                revisions.append(versionrev)
                                conflictingRevisions[repo]['revision'] = versionrev

                        del conflictingEntries
                        newerRevision = max(revisions)
                        if not newerRevision in revisions_duplicates:

                            for reponame in conflictingRevisions:
                                if conflictingRevisions[reponame]['revision'] == newerRevision:
                                    break
                            dbpkginfo = (repoResults[reponame],reponame)

                        else:

                            # ok, we must get the repository with the biggest priority
                            for reponame in valid_repos:
                                if reponame in conflictingRevisions:
                                    break
                            dbpkginfo = (repoResults[reponame],reponame)

        # multimatch support
        if multiMatch:

            if dbpkginfo[1] != 1: # can be "0" or a string, but 1 means failure
                if multiRepo:
                    data = set()
                    for match in dbpkginfo[0]:
                        dbconn = open_db(match[1])
                        matches = dbconn.atomMatch( atom,
                                                    caseSensitive = caseSensitive,
                                                    matchSlot = matchSlot,
                                                    matchBranches = matchBranches,
                                                    packagesFilter = packagesFilter,
                                                    multiMatch = True,
                                                    extendedResults = extendedResults
                                                   )
                        if extendedResults:
                            for item in matches[0]:
                                data.add(((item[0],item[2],item[3],item[4]),match[1]))
                        else:
                            for repoidpackage in matches[0]:
                                data.add((repoidpackage,match[1]))
                    dbpkginfo = (data,0)
                else:
                    dbconn = open_db(dbpkginfo[1])
                    matches = dbconn.atomMatch(
                                                atom,
                                                caseSensitive = caseSensitive,
                                                matchSlot = matchSlot,
                                                matchBranches = matchBranches,
                                                packagesFilter = packagesFilter,
                                                multiMatch = True,
                                                extendedResults = extendedResults
                                               )
                    if extendedResults:
                        dbpkginfo = (set([((x[0],x[2],x[3],x[4]),dbpkginfo[1]) for x in matches[0]]),0)
                    else:
                        dbpkginfo = (set([(x,dbpkginfo[1]) for x in matches[0]]),0)

        if self.xcache:
            try:
                self.dumpTools.dumpobj(etpCache['atomMatch']+c_hash,dbpkginfo)
            except IOError:
                pass

        return dbpkginfo


    def repository_move_clear_cache(self, repoid = None):
        self.clear_dump_cache(etpCache['world_available'])
        self.clear_dump_cache(etpCache['world_update'])
        self.clear_dump_cache(etpCache['check_package_update'])
        self.clear_dump_cache(etpCache['filter_satisfied_deps'])
        self.clear_dump_cache(etpCache['atomMatch'])
        self.clear_dump_cache(etpCache['dep_tree'])
        if repoid != None:
            self.clear_dump_cache(etpCache['dbMatch']+"/"+repoid+"/")
            self.clear_dump_cache(etpCache['dbSearch']+"/"+repoid+"/")


    def addRepository(self, repodata):
        # update etpRepositories
        try:
            etpRepositories[repodata['repoid']] = {}
            etpRepositories[repodata['repoid']]['description'] = repodata['description']
            etpRepositories[repodata['repoid']]['configprotect'] = None
            etpRepositories[repodata['repoid']]['configprotectmask'] = None
        except KeyError:
            t = _("repodata dictionary is corrupted")
            raise exceptionTools.InvalidData("InvalidData: %s" % (t,))

        if repodata['repoid'].endswith(etpConst['packagesext']): # dynamic repository
            try:
                # no need # etpRepositories[repodata['repoid']]['plain_packages'] = repodata['plain_packages'][:]
                etpRepositories[repodata['repoid']]['packages'] = repodata['packages'][:]
                etpRepositories[repodata['repoid']]['smartpackage'] = repodata['smartpackage']
                etpRepositories[repodata['repoid']]['dbpath'] = repodata['dbpath']
                etpRepositories[repodata['repoid']]['pkgpath'] = repodata['pkgpath']
            except KeyError:
                raise exceptionTools.InvalidData("InvalidData: repodata dictionary is corrupted")
            # put at top priority, shift others
            etpRepositoriesOrder.insert(0,repodata['repoid'])
        else:
            # XXX it's boring to keep this in sync with entropyConstants stuff, solutions?
            etpRepositories[repodata['repoid']]['plain_packages'] = repodata['plain_packages'][:]
            etpRepositories[repodata['repoid']]['packages'] = [x+"/"+etpConst['product'] for x in repodata['plain_packages']]
            etpRepositories[repodata['repoid']]['plain_database'] = repodata['plain_database']
            etpRepositories[repodata['repoid']]['database'] = repodata['plain_database'] + "/" + etpConst['product'] + "/database/" + etpConst['currentarch']
            etpRepositories[repodata['repoid']]['dbcformat'] = repodata['dbcformat']
            etpRepositories[repodata['repoid']]['dbpath'] = etpConst['etpdatabaseclientdir'] + "/" + repodata['repoid'] + "/" + etpConst['product'] + "/" + etpConst['currentarch']
            # set dbrevision
            myrev = self.get_repository_revision(repodata['repoid'])
            if myrev == -1:
                myrev = 0
            etpRepositories[repodata['repoid']]['dbrevision'] = str(myrev)
            if repodata.has_key("position"):
                etpRepositoriesOrder.insert(repodata['position'],repodata['repoid'])
            else:
                etpRepositoriesOrder.append(repodata['repoid'])
            if repodata.has_key("service_port"):
                etpRepositories[repodata['repoid']]['service_port'] = repodata['service_port']
            else:
                etpRepositories[repodata['repoid']]['service_port'] = int(etpConst['socket_service']['port'])
            self.repository_move_clear_cache(repodata['repoid'])
            # save new etpRepositories to file
            self.entropyTools.saveRepositorySettings(repodata)
            initConfig_entropyConstants(etpSys['rootdir'])
        self.validate_repositories()

    def removeRepository(self, repoid, disable = False):

        done = False
        if etpRepositories.has_key(repoid):
            del etpRepositories[repoid]
            done = True

        if etpRepositoriesExcluded.has_key(repoid):
            del etpRepositoriesExcluded[repoid]
            done = True

        if done:

            if repoid in etpRepositoriesOrder:
                etpRepositoriesOrder.remove(repoid)

            self.repository_move_clear_cache(repoid)
            # save new etpRepositories to file
            repodata = {}
            repodata['repoid'] = repoid
            if disable:
                self.entropyTools.saveRepositorySettings(repodata, disable = True)
            else:
                self.entropyTools.saveRepositorySettings(repodata, remove = True)
            initConfig_entropyConstants(etpSys['rootdir'])

        self.validate_repositories()

    def shiftRepository(self, repoid, toidx):
        # update etpRepositoriesOrder
        etpRepositoriesOrder.remove(repoid)
        etpRepositoriesOrder.insert(toidx,repoid)
        self.entropyTools.writeOrderedRepositoriesEntries()
        initConfig_entropyConstants(etpSys['rootdir'])
        self.repository_move_clear_cache(repoid)
        self.validate_repositories()

    def enableRepository(self, repoid):
        self.repository_move_clear_cache(repoid)
        # save new etpRepositories to file
        repodata = {}
        repodata['repoid'] = repoid
        self.entropyTools.saveRepositorySettings(repodata, enable = True)
        initConfig_entropyConstants(etpSys['rootdir'])
        self.validate_repositories()

    def disableRepository(self, repoid):
        # update etpRepositories
        done = False
        try:
            del etpRepositories[repoid]
            done = True
        except:
            pass

        if done:
            try:
                etpRepositoriesOrder.remove(repoid)
            except:
                pass
            # it's not vital to reset etpRepositoriesOrder counters

            self.repository_move_clear_cache(repoid)
            # save new etpRepositories to file
            repodata = {}
            repodata['repoid'] = repoid
            self.entropyTools.saveRepositorySettings(repodata, disable = True)
            initConfig_entropyConstants(etpSys['rootdir'])
        self.validate_repositories()


    '''
    @description: filter the already installed dependencies
    @input dependencies: list of dependencies to check
    @output: filtered list, aka the needed ones and the ones satisfied
    '''
    def filterSatisfiedDependencies(self, dependencies, deep_deps = False):

        if self.xcache:
            c_data = list(dependencies)
            c_data.sort()
            client_checksum = self.clientDbconn.database_checksum()
            c_hash = str(hash(tuple(c_data)))+str(hash(deep_deps))+client_checksum
            c_hash = str(hash(c_hash))
            del c_data
            cached = self.dumpTools.loadobj(etpCache['filter_satisfied_deps']+c_hash)
            if cached != None:
                return cached

        unsatisfiedDeps = set()
        satisfiedDeps = set()

        for dependency in dependencies:

            depsatisfied = set()
            depunsatisfied = set()

            ### conflict
            if dependency[0] == "!":
                testdep = dependency[1:]
                xmatch = self.clientDbconn.atomMatch(testdep)
                if xmatch[0] != -1:
                    unsatisfiedDeps.add(dependency)
                else:
                    satisfiedDeps.add(dependency)
                continue

            repoMatch = self.atomMatch(dependency)
            if repoMatch[0] != -1:
                dbconn = self.openRepositoryDatabase(repoMatch[1])
                repo_pkgver = dbconn.retrieveVersion(repoMatch[0])
                repo_pkgtag = dbconn.retrieveVersionTag(repoMatch[0])
                repo_pkgrev = dbconn.retrieveRevision(repoMatch[0])
            else:
                # dependency does not exist in our database
                unsatisfiedDeps.add(dependency)
                continue

            clientMatch = self.clientDbconn.atomMatch(dependency)
            if clientMatch[0] != -1:

                try:
                    installedVer = self.clientDbconn.retrieveVersion(clientMatch[0])
                    installedTag = self.clientDbconn.retrieveVersionTag(clientMatch[0])
                    installedRev = self.clientDbconn.retrieveRevision(clientMatch[0])
                except TypeError: # corrupted entry?
                    installedVer = "0"
                    installedTag = ''
                    installedRev = 0
                #if installedRev == 9999: # any revision is fine
                #    repo_pkgrev = 9999

                if (deep_deps):
                    vcmp = self.entropyTools.entropyCompareVersions(
                                (repo_pkgver,repo_pkgtag,repo_pkgrev),
                                (installedVer,installedTag,installedRev)
                    )
                    if vcmp != 0:
                        depunsatisfied.add(dependency)
                    else:
                        # check if needed is the same?
                        depsatisfied.add(dependency)
                else:
                    depsatisfied.add(dependency)
            else:
                # not the same version installed
                depunsatisfied.add(dependency)

            '''
            if depsatisfied:
                # check if it's really satisfied by looking at needed
                installedNeeded = self.clientDbconn.retrieveNeeded(clientMatch[0])
                repo_needed = dbconn.retrieveNeeded(repoMatch[0])
                if installedNeeded != repo_needed:
                    depunsatisfied.update(depsatisfied)
                    depsatisfied.clear()
            '''

            unsatisfiedDeps |= depunsatisfied
            satisfiedDeps |= depsatisfied

        if self.xcache:
            try:
                self.dumpTools.dumpobj(etpCache['filter_satisfied_deps']+c_hash,(unsatisfiedDeps,satisfiedDeps))
            except IOError:
                pass

        return unsatisfiedDeps, satisfiedDeps


    '''
    @description: generates a dependency tree using unsatisfied dependencies
    @input package: atomInfo (idpackage,reponame)
    @output: dependency tree dictionary, plus status code
    '''
    def generate_dependency_tree(self, atomInfo, empty_deps = False, deep_deps = False, matchfilter = None):

        usefilter = False
        if matchfilter != None:
            usefilter = True

        mydbconn = self.openRepositoryDatabase(atomInfo[1])
        myatom = mydbconn.retrieveAtom(atomInfo[0])

        # caches
        treecache = set()
        matchcache = set()
        keyslotcache = set()
        # special events
        dependenciesNotFound = set()
        conflicts = set()

        mydep = (1,myatom)
        mybuffer = self.entropyTools.lifobuffer()
        deptree = set()
        if usefilter:
            if not matchfilter.inside(atomInfo):
                deptree.add((1,atomInfo))
        else:
            deptree.add((1,atomInfo))

        virgin = True
        while mydep != None:

            # already analyzed in this call
            if mydep[1] in treecache:
                mydep = mybuffer.pop()
                continue

            # conflicts
            if mydep[1][0] == "!":
                xmatch = self.clientDbconn.atomMatch(mydep[1][1:])
                if xmatch[0] != -1:
                    myreplacement = self._lookup_conflict_replacement(mydep[1][1:], xmatch[0], deep_deps = deep_deps)
                    if myreplacement != None:
                        mybuffer.push((mydep[0]+1,myreplacement))
                    else:
                        conflicts.add(xmatch[0])
                mydep = mybuffer.pop()
                continue

            # atom found?
            if virgin:
                virgin = False
                match = atomInfo
            else:
                match = self.atomMatch(mydep[1])
            if match[0] == -1:
                dependenciesNotFound.add(mydep[1])
                mydep = mybuffer.pop()
                continue

            # check if atom has been already pulled in
            matchdb = self.openRepositoryDatabase(match[1])
            matchatom = matchdb.retrieveAtom(match[0])
            matchkey, matchslot = matchdb.retrieveKeySlot(match[0])
            if matchatom in treecache:
                mydep = mybuffer.pop()
                continue
            else:
                treecache.add(matchatom)

            treecache.add(mydep[1])

            # check if key + slot has been already pulled in
            if (matchslot,matchkey) in keyslotcache:
                mydep = mybuffer.pop()
                continue
            else:
                keyslotcache.add((matchslot,matchkey))

            # already analyzed by the calling function
            if usefilter:
                if matchfilter.inside(match):
                    mydep = mybuffer.pop()
                    continue
                matchfilter.add(match)

            # result already analyzed?
            if match in matchcache:
                mydep = mybuffer.pop()
                continue

            treedepth = mydep[0]+1

            # all checks passed, well done
            matchcache.add(match)
            deptree.add((mydep[0],match)) # add match

            # extra hooks
            clientmatch = self.clientDbconn.atomMatch(matchkey, matchSlot = matchslot)
            if clientmatch[0] != -1:
                broken_atoms = self._lookup_library_breakages(match, clientmatch, deep_deps = deep_deps)
                inverse_deps = self._lookup_inverse_dependencies(match, clientmatch)
                if inverse_deps:
                    deptree.remove((mydep[0],match))
                    for ikey,islot in inverse_deps:
                        if (ikey,islot) not in keyslotcache:
                            mybuffer.push((mydep[0],ikey+":"+islot))
                            keyslotcache.add((ikey,islot))
                    deptree.add((treedepth,match))
                    treedepth += 1
                for x in broken_atoms:
                    if x not in treecache:
                        mybuffer.push((treedepth,x))
                        #treecache.add(x) DO NOT DO THIS

            myundeps = matchdb.retrieveDependenciesList(match[0])
            if (not empty_deps):
                myundeps, xxx = self.filterSatisfiedDependencies(myundeps, deep_deps = deep_deps)
                del xxx
            for x in myundeps:
                mybuffer.push((treedepth,x))

            mydep = mybuffer.pop()

        newdeptree = {}
        for x in deptree:
            key = x[0]
            item = x[1]
            if not newdeptree.has_key(key):
                newdeptree[key] = set()
            newdeptree[key].add(item)
        del deptree

        if (dependenciesNotFound):
            # Houston, we've got a problem
            flatview = list(dependenciesNotFound)
            return flatview,-2

        # conflicts
        newdeptree[0] = conflicts

        treecache.clear()
        matchcache.clear()

        return newdeptree,0 # note: newtree[0] contains possible conflicts

    def _lookup_conflict_replacement(self, conflict_atom, client_idpackage, deep_deps):
        if self.entropyTools.isjustname(conflict_atom):
            return None
        conflict_match = self.atomMatch(conflict_atom)
        mykey, myslot = self.clientDbconn.retrieveKeySlot(client_idpackage)
        new_match = self.atomMatch(mykey, matchSlot = myslot)
        if (conflict_match == new_match) or (new_match[1] == 1):
            return None
        action = self.get_package_action(new_match)
        if (action == 0) and (not deep_deps):
            return None
        return "%s:%s" % (mykey,myslot,)

    def _lookup_inverse_dependencies(self, match, clientmatch):

        cmpstat = self.get_package_action(match)
        if cmpstat == 0:
            return set()

        keyslots = set()
        mydepends = self.clientDbconn.retrieveDepends(clientmatch[0])
        for idpackage in mydepends:
            key, slot = self.clientDbconn.retrieveKeySlot(idpackage)
            if (key,slot) in keyslots:
                continue
            # grab its deps
            mydeps = self.clientDbconn.retrieveDependencies(idpackage)
            found = False
            for mydep in mydeps:
                mymatch = self.atomMatch(mydep)
                if mymatch[0] == match[0]:
                    found = True
                    break
            if not found:
                mymatch = self.atomMatch(key, matchSlot = slot)
                if mymatch[0] == -1:
                    continue
                cmpstat = self.get_package_action(mymatch)
                if cmpstat == 0:
                    continue
                keyslots.add((key,slot))

        return keyslots

    def _lookup_library_breakages(self, match, clientmatch, deep_deps = False):

        # there is no need to update this cache when "match" will be installed, because at that point
        # clientmatch[0] will differ.
        if self.xcache:
            c_hash = str(hash(tuple(match)))+str(hash(deep_deps))+str(hash(clientmatch[0]))
            c_hash = str(hash(c_hash))
            try:
                cached = self.dumpTools.loadobj(etpCache['library_breakage']+c_hash)
                if cached != None:
                    return cached
            except (IOError,OSError):
                pass

        # these should be pulled in before
        repo_atoms = set()
        # these can be pulled in after
        client_atoms = set()

        matchdb = self.openRepositoryDatabase(match[1])
        reponeeded = matchdb.retrieveNeeded(match[0], extended = True, format = True) # use extended = True in future
        clientneeded = self.clientDbconn.retrieveNeeded(clientmatch[0], extended = True, format = True) # use extended = True in future
        repo_split = [x.split(".so")[0] for x in reponeeded]
        client_split = [x.split(".so")[0] for x in clientneeded]
        client_side = [x for x in clientneeded if (x not in reponeeded) and (x.split(".so")[0] in repo_split)]
        repo_side = [x for x in reponeeded if (x not in clientneeded) and (x.split(".so")[0] in client_split)]
        del clientneeded,client_split,repo_split

        # all the packages in client_side should be pulled in and updated
        client_idpackages = set()
        for needed in client_side:
            client_idpackages |= self.clientDbconn.searchNeeded(needed)

        client_keyslots = set()
        for idpackage in client_idpackages:
            if idpackage == clientmatch[0]:
                continue
            key, slot = self.clientDbconn.retrieveKeySlot(idpackage)
            client_keyslots.add((key,slot))

        # all the packages in repo_side should be pulled in too
        repodata = {}
        for needed in repo_side:
            repodata[needed] = reponeeded[needed]
        del repo_side,reponeeded

        repo_dependencies = matchdb.retrieveDependencies(match[0])
        matched_deps = set()
        matched_repos = set()
        for dependency in repo_dependencies:
            depmatch = self.atomMatch(dependency)
            if depmatch[0] == -1:
                continue
            matched_repos.add(depmatch[1])
            matched_deps.add(depmatch)

        matched_repos = [x for x in etpRepositoriesOrder if x in matched_repos]
        found_matches = set()
        for needed in repodata:
            for myrepo in matched_repos:
                mydbc = self.openRepositoryDatabase(myrepo)
                solved_needed = mydbc.resolveNeeded(needed, elfclass = repodata[needed])
                found = False
                for idpackage,myfile in solved_needed:
                    x = (idpackage,myrepo)
                    if x in matched_deps:
                        found_matches.add(x)
                        found = True
                        break
                if found:
                    break

        for idpackage,repo in found_matches:
            if not deep_deps:
                cmpstat = self.get_package_action((idpackage,repo))
                if cmpstat == 0:
                    continue
            mydbc = self.openRepositoryDatabase(repo)
            repo_atoms.add(mydbc.retrieveAtom(idpackage))

        for key, slot in client_keyslots:
            idpackage, repo = self.atomMatch(key, matchSlot = slot)
            if idpackage == -1:
                continue
            if not deep_deps:
                cmpstat = self.get_package_action((idpackage, repo))
                if cmpstat == 0:
                    continue
            mydbc = self.openRepositoryDatabase(repo)
            client_atoms.add(mydbc.retrieveAtom(idpackage))

        client_atoms |= repo_atoms

        if self.xcache:
            try:
                self.dumpTools.dumpobj(etpCache['library_breakage']+c_hash,client_atoms)
            except (OSError,IOError):
                pass

        return client_atoms


    def get_required_packages(self, matched_atoms, empty_deps = False, deep_deps = False):

        # clear masking reasons
        maskingReasonsStorage.clear()

        if self.xcache:
            c_data = list(matched_atoms)
            c_data.sort()
            client_checksum = self.clientDbconn.database_checksum()
            c_hash = str(hash(tuple(c_data)))+str(hash(empty_deps))+str(hash(deep_deps))+client_checksum
            c_hash = str(hash(c_hash))
            del c_data
            cached = self.dumpTools.loadobj(etpCache['dep_tree']+c_hash)
            if cached != None:
                return cached

        deptree = {}
        deptree[0] = set()

        atomlen = len(matched_atoms); count = 0
        matchfilter = matchContainer()
        error_generated = 0
        error_tree = set()

        sort_dep_text = _("Sorting dependencies")
        for atomInfo in matched_atoms:

            count += 1
            if (count%10 == 0) or (count == atomlen) or (count == 1):
                self.updateProgress(sort_dep_text, importance = 0, type = "info", back = True, header = ":: ", footer = " ::", percent = True, count = (count,atomlen))

            # check if atomInfo is in matchfilter
            newtree, result = self.generate_dependency_tree(atomInfo, empty_deps, deep_deps, matchfilter = matchfilter)

            if result == -2: # deps not found
                error_generated = -2
                error_tree |= set(newtree) # it is a list, we convert it into set and update error_tree
            elif (result != 0):
                return newtree, result
            elif (newtree):
                parent_keys = deptree.keys()
                # add conflicts
                max_parent_key = parent_keys[-1]
                deptree[0].update(newtree[0])
                # reverse dict
                levelcount = 0
                reversetree = {}
                for key in newtree.keys()[::-1]:
                    if key == 0:
                        continue
                    levelcount += 1
                    reversetree[levelcount] = newtree[key]
                del newtree
                for mylevel in reversetree.keys():
                    deptree[max_parent_key+mylevel] = reversetree[mylevel].copy()
                del reversetree

        matchfilter.clear()
        del matchfilter

        if error_generated != 0:
            return error_tree,error_generated

        if self.xcache:
            try:
                self.dumpTools.dumpobj(etpCache['dep_tree']+c_hash,(deptree,0))
            except IOError:
                pass
        return deptree,0

    def _filter_depends_multimatched_atoms(self, idpackage, depends, monotree):
        remove_depends = set()
        for d_idpackage in depends:
            mydeps = self.clientDbconn.retrieveDependencies(d_idpackage)
            for mydep in mydeps:
                matches, rslt = self.clientDbconn.atomMatch(mydep, multiMatch = True)
                if rslt == 1:
                    continue
                if idpackage in matches and len(matches) > 1:
                    # are all in depends?
                    for mymatch in matches:
                        if mymatch not in depends and mymatch not in monotree:
                            remove_depends.add(d_idpackage)
                            break
        depends -= remove_depends
        return depends

    '''
    @description: generates a depends tree using provided idpackages (from client database)
                    !!! you can see it as the function that generates the removal tree
    @input package: idpackages list
    @output: 	depends tree dictionary, plus status code
    '''
    def generate_depends_tree(self, idpackages, deep = False):

        if self.xcache:
            c_data = list(idpackages)
            c_data.sort()
            c_hash = str(hash(tuple(c_data))) + str(hash(deep))
            c_hash = str(hash(c_hash))
            del c_data
            cached = self.dumpTools.loadobj(etpCache['depends_tree']+c_hash)
            if cached != None:
                return cached

        dependscache = set()
        treeview = set(idpackages)
        treelevel = set(idpackages)
        tree = {}
        treedepth = 0 # I start from level 1 because level 0 is idpackages itself
        tree[treedepth] = set(idpackages)
        monotree = set(idpackages) # monodimensional tree

        # check if dependstable is sane before beginning
        self.clientDbconn.retrieveDepends(idpackages[0])
        count = 0

        rem_dep_text = _("Calculating removable depends of")
        while 1:
            treedepth += 1
            tree[treedepth] = set()
            for idpackage in treelevel:

                count += 1
                p_atom = self.clientDbconn.retrieveAtom(idpackage)
                self.updateProgress(
                    blue(rem_dep_text + " %s" % (red(p_atom),)),
                    importance = 0,
                    type = "info",
                    back = True,
                    header = '|/-\\'[count%4]+" "
                )

                systempkg = self.clientDbconn.isSystemPackage(idpackage)
                if (idpackage in dependscache) or systempkg:
                    if idpackage in treeview:
                        treeview.remove(idpackage)
                    continue

                # obtain its depends
                depends = self.clientDbconn.retrieveDepends(idpackage)
                # filter already satisfied ones
                depends = set([x for x in depends if x not in monotree])
                depends = set([x for x in depends if not self.clientDbconn.isSystemPackage(x)])
                if depends:
                    depends = self._filter_depends_multimatched_atoms(idpackage, depends, monotree)
                if depends: # something depends on idpackage
                    tree[treedepth] |= depends
                    monotree |= depends
                    treeview |= depends
                elif deep: # if deep, grab its dependencies and check

                    mydeps = set()
                    for x in self.clientDbconn.retrieveDependencies(idpackage):
                        match = self.clientDbconn.atomMatch(x)
                        if match[0] != -1:
                            mydeps.add(match[0])

                    # now filter them
                    mydeps = [x for x in mydeps if x not in monotree and (not self.clientDbconn.isSystemPackage(x))]
                    for x in mydeps:
                        mydepends = self.clientDbconn.retrieveDepends(x)
                        mydepends -= set([y for y in mydepends if y not in monotree])
                        if not mydepends:
                            tree[treedepth].add(x)
                            monotree.add(x)
                            treeview.add(x)

                dependscache.add(idpackage)
                if idpackage in treeview:
                    treeview.remove(idpackage)

            treelevel = treeview.copy()
            if not treelevel:
                if not tree[treedepth]:
                    del tree[treedepth] # probably the last one is empty then
                break

        newtree = tree.copy() # tree list
        if (tree):
            # now filter newtree
            treelength = len(newtree)
            for count in range(treelength)[::-1]:
                x = 0
                while x < count:
                    # remove dups in this list
                    for z in newtree[count]:
                        try:
                            while 1:
                                newtree[x].remove(z)
                        except:
                            pass
                    x += 1

        del tree

        if self.xcache:
            try:
                self.dumpTools.dumpobj(etpCache['depends_tree']+c_hash,(newtree,0))
            except IOError:
                pass
        return newtree,0 # treeview is used to show deps while tree is used to run the dependency code.

    def list_repo_categories(self):
        categories = set()
        for repo in self.validRepositories:
            dbconn = self.openRepositoryDatabase(repo)
            catsdata = dbconn.listAllCategories()
            categories.update(set([x[1] for x in catsdata]))
        return categories

    def list_repo_packages_in_category(self, category):
        pkg_matches = set()
        for repo in self.validRepositories:
            dbconn = self.openRepositoryDatabase(repo)
            catsdata = dbconn.searchPackagesByCategory(category, branch = etpConst['branch'])
            pkg_matches.update(set([(x[1],repo) for x in catsdata]))
        return pkg_matches

    def get_category_description_data(self, category, repo = etpConst['officialrepositoryid']):
        dbconn = self.openRepositoryDatabase(repo)
        data = {}
        try:
            data = dbconn.retrieveCategoryDescription(category)
        except dbapi2.OperationalError:
            pass
        if not data:
            for repo in self.validRepositories:
                dbconn = self.openRepositoryDatabase(repo)
                data = dbconn.retrieveCategoryDescription(category)
                if data:
                    break
        return data

    def list_installed_packages_in_category(self, category):
        pkg_matches = set([x[1] for x in self.clientDbconn.searchPackagesByCategory(category)])
        return pkg_matches

    def all_repositories_checksum(self):
        sum_hashes = ''
        for repo in self.validRepositories:
            try:
                dbconn = self.openRepositoryDatabase(repo)
            except (exceptionTools.RepositoryError):
                continue # repo not available
            try:
                sum_hashes += dbconn.database_checksum()
            except dbapi2.OperationalError:
                pass
        return sum_hashes

    def get_available_packages_chash(self, branch):
        repo_digest = self.all_repositories_checksum()
        # client digest not needed, cache is kept updated
        c_hash = str(hash(repo_digest)) + \
                 str(hash(branch)) + \
                 str(hash(tuple(self.validRepositories)))
        c_hash = str(hash(c_hash))
        return c_hash

    # this function searches all the not installed packages available in the repositories
    def calculate_available_packages(self):

        # clear masking reasons
        maskingReasonsStorage.clear()

        if self.xcache:
            c_hash = self.get_available_packages_chash(etpConst['branch'])
            disk_cache = self.dumpTools.loadobj(etpCache['world_available'])
            if disk_cache != None:
                try:
                    if disk_cache['chash'] == c_hash:
                        return disk_cache['available']
                except KeyError:
                    pass

        available = []
        self.setTotalCycles(len(self.validRepositories))
        avail_dep_text = _("Calculating available packages for")
        for repo in self.validRepositories:
            try:
                dbconn = self.openRepositoryDatabase(repo)
                dbconn.validateDatabase()
            except (exceptionTools.RepositoryError,exceptionTools.SystemDatabaseError):
                self.cycleDone()
                continue
            idpackages = dbconn.listAllIdpackages(branch = etpConst['branch'], branch_operator = "<=", order_by = 'atom')
            count = 0
            maxlen = len(idpackages)
            for idpackage in idpackages:
                count += 1
                self.updateProgress(
                    avail_dep_text + " %s" % (repo,),
                    importance = 0,
                    type = "info",
                    back = True,
                    header = "::",
                    count = (count,maxlen),
                    percent = True,
                    footer = " ::"
                )
                # ignore masked packages
                idpackage, idreason = dbconn.idpackageValidator(idpackage)
                if idpackage == -1:
                    continue
                # get key + slot
                key, slot = dbconn.retrieveKeySlot(idpackage)
                matches = self.clientDbconn.searchKeySlot(key, slot)
                if not matches:
                    available.append((idpackage,repo))
            self.cycleDone()

        if self.xcache:
            try:
                data = {}
                data['chash'] = c_hash
                data['available'] = available
                self.dumpTools.dumpobj(etpCache['world_available'],data)
            except IOError:
                pass
        return available

    def get_world_update_cache(self, empty_deps, branch = etpConst['branch'], db_digest = None):
        if self.xcache:
            if db_digest == None:
                db_digest = self.all_repositories_checksum()
            c_hash = self.get_world_update_cache_hash(db_digest, empty_deps, branch)
            disk_cache = self.dumpTools.loadobj(etpCache['world_update']+c_hash)
            if disk_cache != None:
                try:
                    return disk_cache['r']
                except (KeyError, TypeError):
                    return None

    def get_world_update_cache_hash(self, db_digest, empty_deps, branch):
        c_hash = str(hash(db_digest)) + \
                    str(hash(empty_deps)) + \
                    str(hash(tuple(self.validRepositories))) + \
                    str(hash(tuple(etpRepositoriesOrder))) + \
                    str(hash(branch))
        return str(hash(c_hash))

    def calculate_world_updates(self, empty_deps = False, branch = etpConst['branch']):

        # clear masking reasons
        maskingReasonsStorage.clear()

        db_digest = self.all_repositories_checksum()
        cached = self.get_world_update_cache(empty_deps = empty_deps, branch = branch, db_digest = db_digest)
        if cached != None:
            return cached

        update = []
        remove = []
        fine = []

        # get all the installed packages
        idpackages = self.clientDbconn.listAllIdpackages(order_by = 'atom')
        maxlen = len(idpackages)
        count = 0
        for idpackage in idpackages:

            count += 1
            if (count%10 == 0) or (count == maxlen) or (count == 1):
                self.updateProgress(
                    _("Calculating world packages"),
                    importance = 0,
                    type = "info",
                    back = True,
                    header = ":: ",
                    count = (count,maxlen),
                    percent = True,
                    footer = " ::"
                )

            mystrictdata = self.clientDbconn.getStrictData(idpackage)
            # check against broken entries, or removed during iteration
            if mystrictdata == None:
                continue
            try:
                match = self.atomMatch(     mystrictdata[0],
                                            matchSlot = mystrictdata[1],
                                            matchBranches = (branch,),
                                            extendedResults = True
                                    )
            except dbapi2.OperationalError:
                # ouch, but don't crash here
                continue
            # now compare
            # version: mystrictdata[2]
            # tag: mystrictdata[3]
            # revision: mystrictdata[4]
            if (match[0][0] != -1):
                # version: match[0][1]
                # tag: match[0][2]
                # revision: match[0][3]
                if empty_deps:
                    update.append((match[0][0],match[1]))
                    continue
                elif (mystrictdata[2] != match[0][1]):
                    # different versions
                    update.append((match[0][0],match[1]))
                    continue
                elif (mystrictdata[3] != match[0][2]):
                    # different tags
                    update.append((match[0][0],match[1]))
                    continue
                elif (mystrictdata[4] != match[0][3]):
                    # different revision
                    update.append((match[0][0],match[1]))
                    continue
                else:
                    # no difference
                    fine.append(mystrictdata[5])
                    continue

            # don't take action if it's just masked
            maskedresults = self.atomMatch(mystrictdata[0], matchSlot = mystrictdata[1], matchBranches = (branch,), packagesFilter = False)
            if maskedresults[0] == -1:
                remove.append(idpackage)
                # look for packages that would match key with any slot (for eg: gcc, kernel updates)
                matchresults = self.atomMatch(mystrictdata[0], matchBranches = (branch,))
                if matchresults[0] != -1:
                    update.append(matchresults)

        if self.xcache:
            c_hash = self.get_world_update_cache_hash(db_digest, empty_deps, branch)
            data = {}
            data['r'] = (update, remove, fine,)
            data['empty_deps'] = empty_deps
            try:
                self.dumpTools.dumpobj(etpCache['world_update']+c_hash, data)
            except IOError:
                pass
        return update, remove, fine

    def get_match_conflicts(self, match):
        dbconn = self.openRepositoryDatabase(match[1])
        conflicts = dbconn.retrieveConflicts(match[0])
        found_conflicts = set()
        for conflict in conflicts:
            match = self.clientDbconn.atomMatch(conflict)
            if match[0] != -1:
                found_conflicts.add(match[0])
        return found_conflicts

    def is_match_masked(self, match):
        dbconn = self.openRepositoryDatabase(match[1])
        idpackage, idreason = dbconn.idpackageValidator(match[0])
        if idpackage != -1:
            return False
        return True

    # every tbz2 file that would be installed must pass from here
    def add_tbz2_to_repos(self, tbz2file):
        atoms_contained = []
        basefile = os.path.basename(tbz2file)
        if os.path.isdir(etpConst['entropyunpackdir']+"/"+basefile[:-5]):
            shutil.rmtree(etpConst['entropyunpackdir']+"/"+basefile[:-5])
        os.makedirs(etpConst['entropyunpackdir']+"/"+basefile[:-5])
        dbfile = self.entropyTools.extractEdb(tbz2file, dbpath = etpConst['entropyunpackdir']+"/"+basefile[:-5]+"/packages.db")
        if dbfile == None:
            return -1,atoms_contained
        etpSys['dirstoclean'].add(os.path.dirname(dbfile))
        # add dbfile
        repodata = {}
        repodata['repoid'] = basefile
        repodata['description'] = "Dynamic database from "+basefile
        repodata['packages'] = []
        repodata['dbpath'] = os.path.dirname(dbfile)
        repodata['pkgpath'] = os.path.realpath(tbz2file) # extra info added
        repodata['smartpackage'] = False # extra info added
        self.addRepository(repodata)
        mydbconn = self.openGenericDatabase(dbfile)
        # read all idpackages
        try:
            myidpackages = mydbconn.listAllIdpackages() # all branches admitted from external files
        except:
            del etpRepositories[basefile]
            return -2,atoms_contained
        if len(myidpackages) > 1:
            etpRepositories[basefile]['smartpackage'] = True
        for myidpackage in myidpackages:
            compiled_arch = mydbconn.retrieveDownloadURL(myidpackage)
            if compiled_arch.find("/"+etpSys['arch']+"/") == -1:
                return -3,atoms_contained
            atoms_contained.append((int(myidpackage),basefile))
        mydbconn.closeDB()
        del mydbconn
        return 0,atoms_contained

    # This is the function that should be used by third party applications
    # to retrieve a list of available updates, along with conflicts (removalQueue) and obsoletes
    # (removed)
    def retrieveWorldQueue(self, empty_deps = False, branch = etpConst['branch']):
        update, remove, fine = self.calculate_world_updates(empty_deps = empty_deps, branch = branch)
        del fine
        data = {}
        data['removed'] = list(remove)
        data['runQueue'] = []
        data['removalQueue'] = []
        status = -1
        if update:
            # calculate install+removal queues
            install, removal, status = self.retrieveInstallQueue(update, empty_deps, deep_deps = False)
            # update data['removed']
            data['removed'] = [x for x in data['removed'] if x not in removal]
            data['runQueue'] += install
            data['removalQueue'] += removal
        return data,status

    def validatePackageRemoval(self, idpackage):
        system_pkg = self.clientDbconn.isSystemPackage(idpackage)
        if not system_pkg:
            return True # valid

        pkgatom = self.clientDbconn.retrieveAtom(idpackage)
        # check if the package is slotted and exist more than one installed first
        sysresults = self.clientDbconn.atomMatch(self.entropyTools.dep_getkey(pkgatom), multiMatch = True)
        slots = set()
        if sysresults[1] == 0:
            for x in sysresults[0]:
                slots.add(self.clientDbconn.retrieveSlot(x))
            if len(slots) < 2:
                return False
            return True # valid
        else:
            return False


    def retrieveRemovalQueue(self, idpackages, deep = False):
        queue = []
        if not idpackages:
            return queue
        treeview, status = self.generate_depends_tree(idpackages, deep = deep)
        if status == 0:
            for x in range(len(treeview))[::-1]:
                for y in treeview[x]:
                    queue.append(y)
        return queue

    def retrieveInstallQueue(self, matched_atoms, empty_deps, deep_deps):

        # clear masking reasons
        maskingReasonsStorage.clear()

        install = []
        removal = []
        treepackages, result = self.get_required_packages(matched_atoms, empty_deps, deep_deps)

        if result == -2:
            return treepackages,removal,result

        # format
        for x in range(len(treepackages)):
            if x == 0:
                # conflicts
                for a in treepackages[x]:
                    removal.append(a)
            else:
                for a in treepackages[x]:
                    install.append(a)

        # filter out packages that are in actionQueue comparing key + slot
        if install and removal:
            myremmatch = {}
            for x in removal:
                # XXX check if stupid users removed idpackage while this whole instance is running
                if not self.clientDbconn.isIDPackageAvailable(x):
                    continue
                myremmatch.update({(self.entropyTools.dep_getkey(self.clientDbconn.retrieveAtom(x)),self.clientDbconn.retrieveSlot(x)): x})
            for packageInfo in install:
                dbconn = self.openRepositoryDatabase(packageInfo[1])
                testtuple = (
                    self.entropyTools.dep_getkey(dbconn.retrieveAtom(packageInfo[0])),
                    dbconn.retrieveSlot(packageInfo[0])
                )
                if testtuple in myremmatch:
                    # remove from removalQueue
                    if myremmatch[testtuple] in removal:
                        removal.remove(myremmatch[testtuple])
                del testtuple
            del myremmatch

        del treepackages
        return install, removal, 0

    # this function searches into client database for a package matching provided key + slot
    # and returns its idpackage or -1 if none found
    def retrieveInstalledIdPackage(self, pkgkey, pkgslot):
        match = self.clientDbconn.atomMatch(pkgkey, matchSlot = pkgslot)
        if match[1] == 0:
            return match[0]
        return -1

    '''
        Package interface :: begin
    '''

    '''
    @description: check if Equo has to download the given package
    @input package: filename to check inside the packages directory -> file, checksum of the package -> checksum
    @output: -1 = should be downloaded, -2 = digest broken (not mandatory), remove & download, 0 = all fine, we don't need to download it
    '''
    def check_needed_package_download(self, filepath, checksum = None):
        # is the file available
        if os.path.isfile(etpConst['entropyworkdir']+"/"+filepath):
            if checksum is None:
                return 0
            else:
                # check digest
                md5res = self.entropyTools.compareMd5(etpConst['entropyworkdir']+"/"+filepath,checksum)
                if (md5res):
                    return 0
                else:
                    return -2
        else:
            return -1

    '''
    @description: download a package into etpConst['packagesbindir'] and check for digest if digest is not False
    @input package: url -> HTTP/FTP url, digest -> md5 hash of the file
    @output: -1 = download error (cannot find the file), -2 = digest error, 0 = all fine
    '''
    def fetch_file(self, url, digest = None, resume = True):
        # remove old
        filename = os.path.basename(url)
        filepath = etpConst['packagesbindir']+"/"+etpConst['branch']+"/"+filename

        # load class
        fetchConn = self.urlFetcher(url, filepath, resume = resume)
        fetchConn.progress = self.progress

        # start to download
        data_transfer = 0
        resumed = False
        try:
            fetchChecksum = fetchConn.download()
            data_transfer = fetchConn.datatransfer
            resumed = fetchConn.resumed
        except KeyboardInterrupt:
            return -4, data_transfer, resumed
        except NameError:
            raise
        except:
            return -1, data_transfer, resumed
        if fetchChecksum == "-3":
            return -3, data_transfer, resumed

        del fetchConn
        if (digest):
            if (fetchChecksum != digest):
                # not properly downloaded
                return -2, data_transfer, resumed
            else:
                return 0, data_transfer, resumed
        return 0, data_transfer, resumed

    def add_failing_mirror(self, mirrorname,increment = 1):
        item = etpRemoteFailures.get(mirrorname)
        if item == None:
            etpRemoteFailures[mirrorname] = increment
        else:
            etpRemoteFailures[mirrorname] += increment # add a failure
        return etpRemoteFailures[mirrorname]

    def get_failing_mirror_status(self, mirrorname):
        item = etpRemoteFailures.get(mirrorname)
        if item == None:
            return 0
        else:
            return item

    '''
    @description: download a package into etpConst['packagesbindir'] passing all the available mirrors
    @input package: repository -> name of the repository, filename -> name of the file to download, digest -> md5 hash of the file
    @output: 0 = all fine, !=0 = error on all the available mirrors
    '''
    def fetch_file_on_mirrors(self, repository, filename, digest = False, verified = False):

        uris = etpRepositories[repository]['packages'][::-1]
        remaining = set(uris[:])

        if verified: # file is already in place, match_checksum set infoDict['verified'] to True
            return 0

        mirrorcount = 0
        for uri in uris:

            if not remaining:
                # tried all the mirrors, quitting for error
                return 3

            mirrorcount += 1
            mirrorCountText = "( mirror #"+str(mirrorcount)+" ) "
            url = uri+"/"+filename

            # check if uri is sane
            if self.get_failing_mirror_status(uri) >= 30:
                # ohohoh!
                etpRemoteFailures[uri] = 30 # set to 30 for convenience
                mytxt = mirrorCountText
                mytxt += blue(" %s: ") % (_("Mirror"),)
                mytxt += red(self.entropyTools.spliturl(url)[1])
                mytxt += " - %s." % (_("maximum failure threshold reached"),)
                self.updateProgress(
                    mytxt,
                    importance = 1,
                    type = "warning",
                    header = red("   ## ")
                )

                if self.get_failing_mirror_status(uri) == 30:
                    self.add_failing_mirror(uri,45) # put to 75 then decrement by 4 so we won't reach 30 anytime soon ahahaha
                else:
                    # now decrement each time this point is reached, if will be back < 30, then equo will try to use it again
                    if self.get_failing_mirror_status(uri) > 31:
                        self.add_failing_mirror(uri,-4)
                    else:
                        # put to 0 - reenable mirror, welcome back uri!
                        etpRemoteFailures[uri] = 0

                if uri in remaining:
                    remaining.remove(uri)
                continue

            do_resume = True
            while 1:
                try:
                    mytxt = mirrorCountText
                    mytxt += blue("%s: ") % (_("Downloading from"),)
                    mytxt += red(self.entropyTools.spliturl(url)[1])
                    # now fetch the new one
                    self.updateProgress(
                        mytxt,
                        importance = 1,
                        type = "warning",
                        header = red("   ## ")
                    )
                    rc, data_transfer, resumed = self.fetch_file(url, digest, do_resume)
                    if rc == 0:
                        mytxt = mirrorCountText
                        mytxt += blue("%s: ") % (_("Successfully downloaded from"),)
                        mytxt += red(self.entropyTools.spliturl(url)[1])
                        mytxt += " %s %s/%s" % (_("at"),self.entropyTools.bytesIntoHuman(data_transfer),_("second"),)
                        self.updateProgress(
                            mytxt,
                            importance = 1,
                            type = "info",
                            header = red("   ## ")
                        )
                        return 0
                    elif resumed:
                        do_resume = False
                        continue
                    else:
                        error_message = mirrorCountText
                        error_message += blue("%s: %s") % (
                            _("Error downloading from"),
                            red(self.entropyTools.spliturl(url)[1]),
                        )
                        # something bad happened
                        if rc == -1:
                            error_message += " - %s." % (_("file not available on this mirror"),)
                        elif rc == -2:
                            self.add_failing_mirror(uri,1)
                            error_message += " - %s." % (_("wrong checksum"),)
                        elif rc == -3:
                            #self.add_failing_mirror(uri,2)
                            error_message += " - not found."
                        elif rc == -4:
                            error_message += " - %s." % (_("discarded download"),)
                        else:
                            self.add_failing_mirror(uri, 5)
                            error_message += " - %s." % (_("unknown reason"),)
                        self.updateProgress(
                                            error_message,
                                            importance = 1,
                                            type = "warning",
                                            header = red("   ## ")
                                        )
                        if rc == -4: # user discarded fetch
                            return 1
                        if uri in remaining:
                            remaining.remove(uri)
                        break
                except KeyboardInterrupt:
                    break
                except:
                    raise
        return 0

    def quickpkg(self, atomstring, savedir = None):
        if savedir == None:
            savedir = etpConst['packagestmpdir']
            if not os.path.isdir(etpConst['packagestmpdir']):
                os.makedirs(etpConst['packagestmpdir'])
        # match package
        match = self.clientDbconn.atomMatch(atomstring)
        if match[0] == -1:
            return -1,None,None
        atom = self.clientDbconn.atomMatch(match[0])
        pkgdata = self.clientDbconn.getPackageData(match[0])
        resultfile = self.quickpkg_handler(pkgdata = pkgdata, dirpath = savedir)
        if resultfile == None:
            return -1,atom,None
        else:
            return 0,atom,resultfile

    def quickpkg_handler(
                                self,
                                pkgdata,
                                dirpath,
                                edb = True,
                                portdbPath = None,
                                fake = False,
                                compression = "bz2",
                                shiftpath = ""
                        ):

        import stat
        import tarfile

        if compression not in ("bz2","","gz"):
            compression = "bz2"

        # getting package info
        pkgtag = ''
        pkgrev = "~"+str(pkgdata['revision'])
        if pkgdata['versiontag']: pkgtag = "#"+pkgdata['versiontag']
        pkgname = pkgdata['name']+"-"+pkgdata['version']+pkgrev+pkgtag # + version + tag
        pkgcat = pkgdata['category']
        #pkgfile = pkgname+etpConst['packagesext']
        dirpath += "/"+pkgname+etpConst['packagesext']
        if os.path.isfile(dirpath):
            os.remove(dirpath)
        tar = tarfile.open(dirpath,"w:"+compression)

        if not fake:

            contents = [x for x in pkgdata['content']]
            id_strings = {}
            contents.sort()

            # collect files
            for path in contents:
                # convert back to filesystem str
                encoded_path = path
                path = path.encode('raw_unicode_escape')
                path = shiftpath+path
                try:
                    exist = os.lstat(path)
                except OSError:
                    continue # skip file
                arcname = path[len(shiftpath):] # remove shiftpath
                if arcname.startswith("/"):
                    arcname = arcname[1:] # remove trailing /
                ftype = pkgdata['content'][encoded_path]
                if str(ftype) == '0': ftype = 'dir' # force match below, '0' means databases without ftype
                if 'dir' == ftype and \
                    not stat.S_ISDIR(exist.st_mode) and \
                    os.path.isdir(path): # workaround for directory symlink issues
                    path = os.path.realpath(path)

                tarinfo = tar.gettarinfo(path, arcname)
                tarinfo.uname = id_strings.setdefault(tarinfo.uid, str(tarinfo.uid))
                tarinfo.gname = id_strings.setdefault(tarinfo.gid, str(tarinfo.gid))

                if stat.S_ISREG(exist.st_mode):
                    tarinfo.type = tarfile.REGTYPE
                    f = open(path)
                    try:
                        tar.addfile(tarinfo, f)
                    finally:
                        f.close()
                else:
                    tar.addfile(tarinfo)

        tar.close()

        # appending xpak metadata
        if etpConst['gentoo-compat']:
            import etpXpak
            Spm = self.Spm()

            gentoo_name = self.entropyTools.remove_tag(pkgname)
            gentoo_name = self.entropyTools.remove_entropy_revision(gentoo_name)
            if portdbPath == None:
                dbdir = Spm.get_vdb_path()+"/"+pkgcat+"/"+gentoo_name+"/"
            else:
                dbdir = portdbPath+"/"+pkgcat+"/"+gentoo_name+"/"
            if os.path.isdir(dbdir):
                tbz2 = etpXpak.tbz2(dirpath)
                tbz2.recompose(dbdir)

        if edb:
            self.inject_entropy_database_into_package(dirpath, pkgdata)

        if os.path.isfile(dirpath):
            return dirpath
        return None

    def inject_entropy_database_into_package(self, package_filename, data):
        dbpath = self.get_tmp_dbpath()
        dbconn = self.openGenericDatabase(dbpath)
        dbconn.initializeDatabase()
        dbconn.addPackage(data, revision = data['revision'])
        dbconn.commitChanges()
        dbconn.closeDB()
        self.entropyTools.aggregateEdb(tbz2file = package_filename, dbfile = dbpath)
        return dbpath

    def get_tmp_dbpath(self):
        dbpath = etpConst['packagestmpdir']+"/"+str(self.entropyTools.getRandomNumber())
        while os.path.isfile(dbpath):
            dbpath = etpConst['packagestmpdir']+"/"+str(self.entropyTools.getRandomNumber())
        return dbpath

    def Package(self):
        conn = PackageInterface(EquoInstance = self)
        return conn

    '''
        Package interface :: end
    '''

    '''
        Source Package Manager Interface :: begin
    '''
    def Spm(self):
        myroot = etpConst['systemroot']
        cached = self.spmCache.get(myroot)
        if cached != None:
            return cached
        conn = SpmInterface(self)
        self.spmCache[myroot] = conn.intf
        return conn.intf

    # This function extracts all the info from a .tbz2 file and returns them
    def extract_pkg_metadata(self, package, etpBranch = etpConst['branch'], silent = False, inject = False):

        data = {}
        info_package = bold(os.path.basename(package))+": "

        if not silent:
            self.updateProgress(
                red(info_package+_("Extacting package metadata")+" ..."),
                importance = 0,
                type = "info",
                header = brown(" * "),
                back = True
            )

        filepath = package
        tbz2File = package
        package = package.split(etpConst['packagesext'])[0]
        package = self.entropyTools.remove_entropy_revision(package)
        package = self.entropyTools.remove_tag(package)
        # remove category
        if package.find(":") != -1:
            package = ':'.join(package.split(":")[1:])

        package = package.split("-")
        pkgname = ""
        pkglen = len(package)
        if package[pkglen-1].startswith("r"):
            pkgver = package[pkglen-2]+"-"+package[pkglen-1]
            pkglen -= 2
        else:
            pkgver = package[-1]
            pkglen -= 1
        for i in range(pkglen):
            if i == pkglen-1:
                pkgname += package[i]
            else:
                pkgname += package[i]+"-"
        pkgname = pkgname.split("/")[-1]

        # Fill Package name and version
        data['name'] = pkgname
        data['version'] = pkgver

        # .tbz2 md5
        data['digest'] = self.entropyTools.md5sum(tbz2File)
        data['datecreation'] = str(self.entropyTools.getFileUnixMtime(tbz2File))
        # .tbz2 byte size
        data['size'] = str(os.stat(tbz2File)[6])

        # unpack file
        tbz2TmpDir = etpConst['packagestmpdir']+"/"+data['name']+"-"+data['version']+"/"
        if not os.path.isdir(tbz2TmpDir):
            if os.path.lexists(tbz2TmpDir):
                os.remove(tbz2TmpDir)
            os.makedirs(tbz2TmpDir)
        self.entropyTools.extractXpak(tbz2File,tbz2TmpDir)

        # Fill chost
        f = open(tbz2TmpDir+etpConst['spm']['xpak_entries']['chost'],"r")
        data['chost'] = f.readline().strip()
        f.close()

        # Fill branch
        data['branch'] = etpBranch

        # Fill description
        data['description'] = ""
        try:
            f = open(tbz2TmpDir+etpConst['spm']['xpak_entries']['description'],"r")
            data['description'] = f.readline().strip()
            f.close()
        except IOError:
            pass

        # Fill homepage
        data['homepage'] = ""
        try:
            f = open(tbz2TmpDir+etpConst['spm']['xpak_entries']['homepage'],"r")
            data['homepage'] = f.readline().strip()
            f.close()
        except IOError:
            pass

        # fill slot, if it is
        data['slot'] = ""
        try:
            f = open(tbz2TmpDir+etpConst['spm']['xpak_entries']['slot'],"r")
            data['slot'] = f.readline().strip()
            f.close()
        except IOError:
            pass

        # fill slot, if it is
        if inject:
            data['injected'] = True
        else:
            data['injected'] = False

        # fill eclasses list
        data['eclasses'] = []
        try:
            f = open(tbz2TmpDir+etpConst['spm']['xpak_entries']['inherited'],"r")
            data['eclasses'] = f.readline().strip().split()
            f.close()
        except IOError:
            pass

        # fill needed list
        data['needed'] = set()
        try:
            f = open(tbz2TmpDir+etpConst['spm']['xpak_entries']['needed'],"r")
            lines = f.readlines()
            f.close()
            for line in lines:
                line = line.strip()
                if line:
                    needed = line.split()
                    if len(needed) == 2:
                        ownlib = needed[0]
                        ownelf = -1
                        if os.access(ownlib,os.R_OK):
                            ownelf = self.entropyTools.read_elf_class(ownlib)
                        libs = needed[1].split(",")
                        for lib in libs:
                            if (lib.find(".so") != -1):
                                data['needed'].add((lib,ownelf))
        except IOError:
            pass
        data['needed'] = list(data['needed'])

        data['content'] = {}
        if os.path.isfile(tbz2TmpDir+etpConst['spm']['xpak_entries']['contents']):
            f = open(tbz2TmpDir+etpConst['spm']['xpak_entries']['contents'],"r")
            content = f.readlines()
            f.close()
            outcontent = set()
            for line in content:
                line = line.strip().split()
                try:
                    datatype = line[0]
                    datafile = line[1:]
                    if datatype == 'obj':
                        datafile = datafile[:-2]
                        datafile = ' '.join(datafile)
                    elif datatype == 'dir':
                        datafile = ' '.join(datafile)
                    elif datatype == 'sym':
                        datafile = datafile[:-3]
                        datafile = ' '.join(datafile)
                    else:
                        myexc = "InvalidData: %s %s. %s." % (
                            datafile,
                            _("not supported"),
                            _("Probably Portage API has changed"),
                        )
                        raise exceptionTools.InvalidData(myexc)
                    outcontent.add((datafile,datatype))
                except:
                    pass

            _outcontent = set()
            for i in outcontent:
                i = list(i)
                datatype = i[1]
                _outcontent.add((i[0],i[1]))
            outcontent = list(_outcontent)
            outcontent.sort()
            for i in outcontent:
                data['content'][i[0]] = i[1]

        else:
            # CONTENTS is not generated when a package is emerged with portage and the option -B
            # we have to unpack the tbz2 and generate content dict
            mytempdir = etpConst['packagestmpdir']+"/"+os.path.basename(filepath)+".inject"
            if os.path.isdir(mytempdir):
                shutil.rmtree(mytempdir)
            if not os.path.isdir(mytempdir):
                os.makedirs(mytempdir)
            self.entropyTools.uncompressTarBz2(filepath, extractPath = mytempdir, catchEmpty = True)

            for currentdir, subdirs, files in os.walk(mytempdir):
                data['content'][currentdir[len(mytempdir):]] = "dir"
                for item in files:
                    item = currentdir+"/"+item
                    if os.path.islink(item):
                        data['content'][item[len(mytempdir):]] = "sym"
                    else:
                        data['content'][item[len(mytempdir):]] = "obj"

            # now remove
            shutil.rmtree(mytempdir,True)
            try:
                os.rmdir(mytempdir)
            except:
                pass

        # files size on disk
        if (data['content']):
            data['disksize'] = 0
            for item in data['content']:
                try:
                    size = os.stat(item)[6]
                    data['disksize'] += size
                except:
                    pass
        else:
            data['disksize'] = 0

        # [][][] Kernel dependent packages hook [][][]
        data['versiontag'] = ''
        kernelstuff = False
        kernelstuff_kernel = False
        for item in data['content']:
            if item.startswith("/lib/modules/"):
                kernelstuff = True
                # get the version of the modules
                kmodver = item.split("/lib/modules/")[1]
                kmodver = kmodver.split("/")[0]

                lp = kmodver.split("-")[-1]
                if lp.startswith("r"):
                    kname = kmodver.split("-")[-2]
                    kver = kmodver.split("-")[0]+"-"+kmodver.split("-")[-1]
                else:
                    kname = kmodver.split("-")[-1]
                    kver = kmodver.split("-")[0]
                break
        # validate the results above
        if (kernelstuff):
            matchatom = "linux-%s-%s" % (kname,kver,)
            if (matchatom == data['name']+"-"+data['version']):
                kernelstuff_kernel = True

        # Fill category
        f = open(tbz2TmpDir+etpConst['spm']['xpak_entries']['category'],"r")
        data['category'] = f.readline().strip()
        f.close()

        # Fill download relative URI
        if (kernelstuff):
            data['versiontag'] = kmodver
            if not kernelstuff_kernel:
                data['slot'] = kmodver # if you change this behaviour,
                                       # you must change "reagent update"
                                       # and "equo database gentoosync" consequentially
            versiontag = "#"+data['versiontag']
        else:
            versiontag = ""
        data['download'] = etpConst['packagesrelativepath'] + data['branch'] + "/"
        data['download'] += data['category']+":"+data['name']+"-"+data['version']
        data['download'] += versiontag+etpConst['packagesext']

        # Fill counter
        try:
            f = open(tbz2TmpDir+etpConst['spm']['xpak_entries']['counter'],"r")
            data['counter'] = int(f.readline().strip())
            f.close()
        except IOError:
            data['counter'] = -2 # -2 values will be insterted as incremental negative values into the database

        data['trigger'] = ""
        if os.path.isfile(etpConst['triggersdir']+"/"+data['category']+"/"+data['name']+"/"+etpConst['triggername']):
            f = open(etpConst['triggersdir']+"/"+data['category']+"/"+data['name']+"/"+etpConst['triggername'],"rb")
            data['trigger'] = f.read()
            f.close()

        # Fill CFLAGS
        data['cflags'] = ""
        try:
            f = open(tbz2TmpDir+etpConst['spm']['xpak_entries']['cflags'],"r")
            data['cflags'] = f.readline().strip()
            f.close()
        except IOError:
            pass

        # Fill CXXFLAGS
        data['cxxflags'] = ""
        try:
            f = open(tbz2TmpDir+etpConst['spm']['xpak_entries']['cxxflags'],"r")
            data['cxxflags'] = f.readline().strip()
            f.close()
        except IOError:
            pass

        # fill KEYWORDS
        data['keywords'] = []
        try:
            f = open(tbz2TmpDir+etpConst['spm']['xpak_entries']['keywords'],"r")
            cnt = f.readline().strip().split()
            if not cnt:
                data['keywords'].append("") # support for packages with no keywords
            else:
                for i in cnt:
                    if i:
                        data['keywords'].append(i)
            f.close()
        except IOError:
            pass


        f = open(tbz2TmpDir+etpConst['spm']['xpak_entries']['rdepend'],"r")
        rdepend = f.readline().strip()
        f.close()

        f = open(tbz2TmpDir+etpConst['spm']['xpak_entries']['pdepend'],"r")
        pdepend = f.readline().strip()
        f.close()

        f = open(tbz2TmpDir+etpConst['spm']['xpak_entries']['depend'],"r")
        depend = f.readline().strip()
        f.close()

        f = open(tbz2TmpDir+etpConst['spm']['xpak_entries']['use'],"r")
        use = f.readline().strip()
        f.close()

        try:
            f = open(tbz2TmpDir+etpConst['spm']['xpak_entries']['iuse'],"r")
            iuse = f.readline().strip()
            f.close()
        except IOError:
            iuse = ""

        try:
            f = open(tbz2TmpDir+etpConst['spm']['xpak_entries']['license'],"r")
            lics = f.readline().strip()
            f.close()
        except IOError:
            lics = ""

        try:
            f = open(tbz2TmpDir+etpConst['spm']['xpak_entries']['provide'],"r")
            provide = f.readline().strip()
        except IOError:
            provide = ""

        try:
            f = open(tbz2TmpDir+etpConst['spm']['xpak_entries']['src_uri'],"r")
            sources = f.readline().strip()
            f.close()
        except IOError:
            sources = ""

        Spm = self.Spm()

        portage_metadata = Spm.calculate_dependencies(iuse, use, lics, depend, rdepend, pdepend, provide, sources)

        data['provide'] = portage_metadata['PROVIDE'].split()
        data['license'] = portage_metadata['LICENSE']
        data['useflags'] = []
        for x in use.split():
            if x in portage_metadata['USE']:
                data['useflags'].append(x)
            else:
                data['useflags'].append("-"+x)
        data['sources'] = portage_metadata['SRC_URI'].split()
        data['dependencies'] = {}
        for x in portage_metadata['RDEPEND'].split():
            if x.startswith("!") or (x in ("(","||",")","")):
                continue
            data['dependencies'][x] = 0
        for x in portage_metadata['PDEPEND'].split():
            if x.startswith("!") or (x in ("(","||",")","")):
                continue
            data['dependencies'][x] = 1
        data['conflicts'] = [x[1:] for x in portage_metadata['RDEPEND'].split()+portage_metadata['PDEPEND'].split() if x.startswith("!") and not x in ("(","||",")","")]

        if (kernelstuff) and (not kernelstuff_kernel):
            # add kname to the dependency
            data['dependencies']["=sys-kernel/linux-"+kname+"-"+kver] = 0
            key = data['category']+"/"+data['name']
            if etpConst['conflicting_tagged_packages'].has_key(key):
                myconflicts = etpConst['conflicting_tagged_packages'][key]
                for conflict in myconflicts:
                    data['conflicts'].append(conflict)

        # Get License text if possible
        licenses_dir = os.path.join(Spm.get_spm_setting('PORTDIR'),'licenses')
        data['licensedata'] = {}
        if licenses_dir:
            licdata = [str(x.strip()) for x in data['license'].split() if str(x.strip()) and self.entropyTools.is_valid_string(x.strip())]
            for mylicense in licdata:

                licfile = os.path.join(licenses_dir,mylicense)
                if os.access(licfile,os.R_OK):
                    if self.entropyTools.istextfile(licfile):
                        f = open(licfile)
                        data['licensedata'][mylicense] = f.read()
                        f.close()

        # manage data['sources'] to create data['mirrorlinks']
        # =mirror://openoffice|link1|link2|link3
        data['mirrorlinks'] = []
        for i in data['sources']:
            if i.startswith("mirror://"):
                # parse what mirror I need
                mirrorURI = i.split("/")[2]
                mirrorlist = Spm.get_third_party_mirrors(mirrorURI)
                data['mirrorlinks'].append([mirrorURI,mirrorlist])
                # mirrorURI = openoffice and mirrorlist = [link1, link2, link3]

        # write only if it's a systempackage
        data['systempackage'] = False
        systemPackages = Spm.get_atoms_in_system()
        for x in systemPackages:
            x = self.entropyTools.dep_getkey(x)
            y = data['category']+"/"+data['name']
            if x == y:
                # found
                data['systempackage'] = True
                break

        # write only if it's a systempackage
        protect, mask = Spm.get_config_protect_and_mask()
        data['config_protect'] = protect
        data['config_protect_mask'] = mask

        # fill data['messages']
        # etpConst['logdir']+"/elog"
        if not os.path.isdir(etpConst['logdir']+"/elog"):
            os.makedirs(etpConst['logdir']+"/elog")
        data['messages'] = []
        if os.path.isdir(etpConst['logdir']+"/elog"):
            elogfiles = os.listdir(etpConst['logdir']+"/elog")
            myelogfile = data['category']+":"+data['name']+"-"+data['version']
            foundfiles = []
            for item in elogfiles:
                if item.startswith(myelogfile):
                    foundfiles.append(item)
            if foundfiles:
                elogfile = foundfiles[0]
                if len(foundfiles) > 1:
                    # get the latest
                    mtimes = []
                    for item in foundfiles:
                        mtimes.append((self.entropyTools.getFileUnixMtime(etpConst['logdir']+"/elog/"+item),item))
                    mtimes.sort()
                    elogfile = mtimes[len(mtimes)-1][1]
                messages = self.entropyTools.extractElog(etpConst['logdir']+"/elog/"+elogfile)
                for message in messages:
                    message = message.replace("emerge","install")
                    data['messages'].append(message)
        else:
            if not silent:
                mytxt = "%s, %s" % (_("not set"),_("have you configured make.conf properly?"),)
                self.updateProgress(
                    red(etpConst['logdir']+"/elog ")+mytxt,
                    importance = 1,
                    type = "warning",
                    header = brown(" * ")
                )

        # write API info
        data['etpapi'] = etpConst['etpapi']

        # removing temporary directory
        shutil.rmtree(tbz2TmpDir,True)
        if os.path.isdir(tbz2TmpDir):
            try:
                os.remove(tbz2TmpDir)
            except OSError:
                pass

        if not silent:
            self.updateProgress(
                red(info_package+_("Package extraction complete")),
                importance = 0,
                type = "info",
                header = brown(" * "),
                back = True
            )
        return data

    '''
        Source Package Manager Interface :: end
    '''

    '''
        Triggers interface :: begindatabaseStructureUpdates
    '''
    def Triggers(self, phase, pkgdata):
        conn = TriggerInterface(EquoInstance = self, phase = phase, pkgdata = pkgdata)
        return conn
    '''
        Triggers interface :: end
    '''

    '''
        Repository interface :: begin
    '''
    def Repositories(self, reponames = [], forceUpdate = False, noEquoCheck = False, fetchSecurity = True):
        conn = RepoInterface(EquoInstance = self, reponames = reponames, forceUpdate = forceUpdate, noEquoCheck = noEquoCheck, fetchSecurity = fetchSecurity)
        return conn
    '''
        Repository interface :: end
    '''

    '''
        Configuration files (updates, not entropy related) interface :: begin
    '''
    def FileUpdatesInterfaceLoader(self):
        conn = FileUpdatesInterface(EquoInstance = self)
        return conn
    '''
        Configuration files (updates, not entropy related) interface :: end
    '''

    def PackageMaskingParserInterfaceLoader(self):
        conn = PackageMaskingParser(EquoInstance = self)
        return conn

'''
    Real package actions (install/remove) interface
'''
class PackageInterface:

    def __init__(self, EquoInstance):

        if not isinstance(EquoInstance,EquoInterface):
            mytxt = _("A valid Equo instance or subclass is needed")
            raise exceptionTools.IncorrectParameter("IncorrectParameter: %s" % (mytxt,))
        self.Entropy = EquoInstance
        self.infoDict = {}
        self.prepared = False
        self.matched_atom = ()
        self.valid_actions = ("fetch","remove","install")
        self.action = None

    def kill(self):
        self.infoDict.clear()
        self.matched_atom = ()
        self.valid_actions = ()
        self.action = None
        self.prepared = False

    def error_on_prepared(self):
        if self.prepared:
            mytxt = _("Already prepared")
            raise exceptionTools.PermissionDenied("PermissionDenied: %s" % (mytxt,))

    def error_on_not_prepared(self):
        if not self.prepared:
            mytxt = _("Not yet prepared")
            raise exceptionTools.PermissionDenied("PermissionDenied: %s" % (mytxt,))

    def check_action_validity(self, action):
        if action not in self.valid_actions:
            mytxt = _("Action must be in")
            raise exceptionTools.InvalidData("InvalidData: %s %s" % (mytxt,self.valid_actions,))

    def match_checksum(self):
        self.error_on_not_prepared()
        dlcount = 0
        match = False
        while dlcount <= 5:
            self.Entropy.updateProgress(
                blue(_("Checking package checksum...")),
                importance = 0,
                type = "info",
                header = red("   ## "),
                back = True
            )
            dlcheck = self.Entropy.check_needed_package_download(self.infoDict['download'], checksum = self.infoDict['checksum'])
            if dlcheck == 0:
                self.Entropy.updateProgress(
                    blue(_("Package checksum matches.")),
                    importance = 0,
                    type = "info",
                    header = red("   ## ")
                )
                self.infoDict['verified'] = True
                match = True
                break # file downloaded successfully
            else:
                dlcount += 1
                self.Entropy.updateProgress(
                    blue(_("Package checksum does not match. Redownloading... attempt #%s") % (dlcount,)),
                    importance = 0,
                    type = "info",
                    header = red("   ## "),
                    back = True
                )
                fetch = self.Entropy.fetch_file_on_mirrors(
                            self.infoDict['repository'],
                            self.infoDict['download'],
                            self.infoDict['checksum']
                        )
                if fetch != 0:
                    self.Entropy.updateProgress(
                        blue(_("Cannot properly fetch package! Quitting.")),
                        importance = 0,
                        type = "info",
                        header = red("   ## ")
                    )
                    return fetch
                else:
                    self.infoDict['verified'] = True
        if (not match):
            mytxt = _("Cannot properly fetch package or checksum does not match. Try download latest repositories.")
            self.Entropy.updateProgress(
                blue(mytxt),
                importance = 0,
                type = "info",
                header = red("   ## ")
            )
            return 1
        return 0

    '''
    @description: unpack the given package file into the unpack dir
    @input infoDict: dictionary containing package information
    @output: 0 = all fine, >0 = error!
    '''
    def __unpack_package(self):

        self.error_on_not_prepared()

        if not self.infoDict['merge_from']:
            self.Entropy.clientLog.log(ETP_LOGPRI_INFO,ETP_LOGLEVEL_NORMAL,"Unpacking package: "+str(self.infoDict['atom']))
        else:
            self.Entropy.clientLog.log(ETP_LOGPRI_INFO,ETP_LOGLEVEL_NORMAL,"Merging package: "+str(self.infoDict['atom']))

        if os.path.isdir(self.infoDict['unpackdir']):
            shutil.rmtree(self.infoDict['unpackdir'].encode('raw_unicode_escape'))
        elif os.path.isfile(self.infoDict['unpackdir']):
            os.remove(self.infoDict['unpackdir'].encode('raw_unicode_escape'))
        os.makedirs(self.infoDict['imagedir'])

        if not os.path.isfile(self.infoDict['pkgpath']) and not self.infoDict['merge_from']:
            if os.path.isdir(self.infoDict['pkgpath']):
                shutil.rmtree(self.infoDict['pkgpath'])
            if os.path.islink(self.infoDict['pkgpath']):
                os.remove(self.infoDict['pkgpath'])
            self.infoDict['verified'] = False
            self.fetch_step()

        if not self.infoDict['merge_from']:
            rc = self.Entropy.entropyTools.spawnFunction(
                        self.Entropy.entropyTools.uncompressTarBz2,
                        self.infoDict['pkgpath'],
                        self.infoDict['imagedir'],
                        catchEmpty = True
                )
            if rc != 0:
                return rc
        else:
            #self.__fill_image_dir(self.infoDict['merge_from'],self.infoDict['imagedir'])
            self.Entropy.entropyTools.spawnFunction(
                        self.__fill_image_dir,
                        self.infoDict['merge_from'],
                        self.infoDict['imagedir']
                )

        # unpack xpak ?
        if etpConst['gentoo-compat']:
            if os.path.isdir(self.infoDict['xpakpath']):
                shutil.rmtree(self.infoDict['xpakpath'])
            try:
                os.rmdir(self.infoDict['xpakpath'])
            except OSError:
                pass

            # create data dir where we'll unpack the xpak
            os.makedirs(self.infoDict['xpakpath']+"/"+etpConst['entropyxpakdatarelativepath'],0755)
            #os.mkdir(self.infoDict['xpakpath']+"/"+etpConst['entropyxpakdatarelativepath'])
            xpakPath = self.infoDict['xpakpath']+"/"+etpConst['entropyxpakfilename']

            if not self.infoDict['merge_from']:
                if (self.infoDict['smartpackage']):
                    # we need to get the .xpak from database
                    xdbconn = self.Entropy.openRepositoryDatabase(self.infoDict['repository'])
                    xpakdata = xdbconn.retrieveXpakMetadata(self.infoDict['idpackage'])
                    if xpakdata:
                        # save into a file
                        f = open(xpakPath,"wb")
                        f.write(xpakdata)
                        f.flush()
                        f.close()
                        self.infoDict['xpakstatus'] = self.Entropy.entropyTools.unpackXpak(
                            xpakPath,
                            self.infoDict['xpakpath']+"/"+etpConst['entropyxpakdatarelativepath']
                        )
                    else:
                        self.infoDict['xpakstatus'] = None
                    del xpakdata
                else:
                    self.infoDict['xpakstatus'] = self.Entropy.entropyTools.extractXpak(
                        self.infoDict['pkgpath'],
                        self.infoDict['xpakpath']+"/"+etpConst['entropyxpakdatarelativepath']
                    )
            else:
                # link xpakdir to self.infoDict['xpakpath']+"/"+etpConst['entropyxpakdatarelativepath']
                tolink_dir = self.infoDict['xpakpath']+"/"+etpConst['entropyxpakdatarelativepath']
                if os.path.isdir(tolink_dir):
                    shutil.rmtree(tolink_dir,True)
                # now link
                os.symlink(self.infoDict['xpakdir'],tolink_dir)

            # create fake portage ${D} linking it to imagedir
            portage_db_fakedir = os.path.join(
                self.infoDict['unpackdir'],
                "portage/"+self.infoDict['category'] + "/" + self.infoDict['name'] + "-" + self.infoDict['version']
            )

            os.makedirs(portage_db_fakedir,0755)
            # now link it to self.infoDict['imagedir']
            os.symlink(self.infoDict['imagedir'],os.path.join(portage_db_fakedir,"image"))

        return 0

    def __remove_package(self):

        # clear on-disk cache
        self.__clear_cache()

        self.Entropy.clientLog.log(ETP_LOGPRI_INFO,ETP_LOGLEVEL_NORMAL,"Removing package: "+str(self.infoDict['removeatom']))

        # remove from database
        if self.infoDict['removeidpackage'] != -1:
            mytxt = "%s: " % (_("Removing from Entropy"),)
            self.Entropy.updateProgress(
                blue(mytxt) + red(self.infoDict['removeatom']),
                importance = 1,
                type = "info",
                header = red("   ## ")
            )
            self.__remove_package_from_database()

        # Handle gentoo database
        if (etpConst['gentoo-compat']):
            gentooAtom = self.Entropy.entropyTools.remove_tag(self.infoDict['removeatom'])
            self.Entropy.clientLog.log(ETP_LOGPRI_INFO,ETP_LOGLEVEL_NORMAL,"Removing from Portage: "+str(gentooAtom))
            self.__remove_package_from_gentoo_database(gentooAtom)
            del gentooAtom

        self.__remove_content_from_system()
        return 0

    def __remove_content_from_system(self):

        # load CONFIG_PROTECT and its mask
        # client database at this point has been surely opened,
        # so our dicts are already filled
        protect = etpConst['dbconfigprotect']
        mask = etpConst['dbconfigprotectmask']

        # remove files from system
        directories = set()
        for item in self.infoDict['removecontent']:
            # collision check
            if etpConst['collisionprotect'] > 0:

                if self.Entropy.clientDbconn.isFileAvailable(item) and os.path.isfile(etpConst['systemroot']+item):
                    # in this way we filter out directories
                    mytxt = red(_("Collision found during removal of")) + " " + etpConst['systemroot']+item + " - "
                    mytxt += red(_("cannot overwrite"))
                    self.Entropy.updateProgress(
                        mytxt,
                        importance = 1,
                        type = "warning",
                        header = red("   ## ")
                    )
                    self.Entropy.clientLog.log(
                        ETP_LOGPRI_INFO,
                        ETP_LOGLEVEL_NORMAL,
                        "Collision found during remove of "+etpConst['systemroot']+item+" - cannot overwrite"
                    )
                    continue

            protected = False
            if (not self.infoDict['removeconfig']) and (not self.infoDict['diffremoval']):
                try:
                    # -- CONFIGURATION FILE PROTECTION --
                    if os.access(etpConst['systemroot']+item,os.R_OK):
                        for x in protect:
                            if etpConst['systemroot']+item.startswith(x):
                                protected = True
                                break
                        if (protected):
                            for x in mask:
                                if etpConst['systemroot']+item.startswith(x):
                                    protected = False
                                    break
                        if (protected) and os.path.isfile(etpConst['systemroot']+item):
                            protected = self.Entropy.entropyTools.istextfile(etpConst['systemroot']+item)
                        else:
                            protected = False # it's not a file
                    # -- CONFIGURATION FILE PROTECTION --
                except:
                    pass # some filenames are buggy encoded


            if protected:
                self.Entropy.clientLog.log(
                    ETP_LOGPRI_INFO,
                    ETP_LOGLEVEL_VERBOSE,
                    "[remove] Protecting config file: "+etpConst['systemroot']+item
                )
                mytxt = "[%s] %s: %s" % (
                    red(_("remove")),
                    brown(_("Protecting config file")),
                    etpConst['systemroot']+item,
                )
                self.Entropy.updateProgress(
                    mytxt,
                    importance = 1,
                    type = "warning",
                    header = red("   ## ")
                )
            else:
                try:
                    os.lstat(etpConst['systemroot']+item)
                except OSError:
                    continue # skip file, does not exist
                except UnicodeEncodeError:
                    mytxt = brown(_("This package contains a badly encoded file !!!"))
                    self.Entropy.updateProgress(
                        red("QA: ")+mytxt,
                        importance = 1,
                        type = "warning",
                        header = darkred("   ## ")
                    )
                    continue # file has a really bad encoding

                if os.path.isdir(etpConst['systemroot']+item) and os.path.islink(etpConst['systemroot']+item):
                    # S_ISDIR returns False for directory symlinks, so using os.path.isdir
                    # valid directory symlink
                    directories.add((etpConst['systemroot']+item,"link"))
                elif os.path.isdir(etpConst['systemroot']+item):
                    # plain directory
                    directories.add((etpConst['systemroot']+item,"dir"))
                else: # files, symlinks or not
                    # just a file or symlink or broken directory symlink (remove now)
                    try:
                        os.remove(etpConst['systemroot']+item)
                        # add its parent directory
                        dirfile = os.path.dirname(etpConst['systemroot']+item)
                        if os.path.isdir(dirfile) and os.path.islink(dirfile):
                            directories.add((dirfile,"link"))
                        elif os.path.isdir(dirfile):
                            directories.add((dirfile,"dir"))
                    except OSError:
                        pass

        # now handle directories
        directories = list(directories)
        directories.reverse()
        while 1:
            taint = False
            for directory in directories:
                mydir = etpConst['systemroot']+directory[0]
                if directory[1] == "link":
                    try:
                        mylist = os.listdir(mydir)
                        if not mylist:
                            try:
                                os.remove(mydir)
                                taint = True
                            except OSError:
                                pass
                    except OSError:
                        pass
                elif directory[1] == "dir":
                    try:
                        mylist = os.listdir(mydir)
                        if not mylist:
                            try:
                                os.rmdir(mydir)
                                taint = True
                            except OSError:
                                pass
                    except OSError:
                        pass

            if not taint:
                break
        del directories


    '''
    @description: remove package entry from Gentoo database
    @input gentoo package atom (cat/name+ver):
    @output: 0 = all fine, <0 = error!
    '''
    def __remove_package_from_gentoo_database(self, atom):

        # handle gentoo-compat
        try:
            Spm = self.Entropy.Spm()
        except:
            return -1 # no Spm support ??

        portDbDir = Spm.get_vdb_path()
        removePath = portDbDir+atom
        key = self.Entropy.entropyTools.dep_getkey(atom)
        others_installed = Spm.search_keys(key)
        slot = self.infoDict['slot']
        tag = self.infoDict['versiontag'] # FIXME: kernel tag, hopefully to 0
        if tag: slot = "0"
        if os.path.isdir(removePath):
            shutil.rmtree(removePath,True)
        elif others_installed:
            for myatom in others_installed:
                myslot = Spm.get_package_slot(myatom)
                if myslot != slot:
                    continue
                shutil.rmtree(portDbDir+myatom,True)

        if not others_installed:
            world_file = etpConst['systemroot']+'/var/lib/portage/world'
            world_file_tmp = world_file+".entropy.tmp"
            if os.access(world_file,os.W_OK) and os.path.isfile(world_file):
                new = open(world_file_tmp,"w")
                old = open(world_file,"r")
                line = old.readline()
                while line:
                    if line.find(key) != -1:
                        line = old.readline()
                        continue
                    if line.find(key+":"+slot) != -1:
                        line = old.readline()
                        continue
                    new.write(line)
                    line = old.readline()
                new.flush()
                new.close()
                old.close()
                shutil.move(world_file_tmp,world_file)
        return 0

    '''
    @description: function that runs at the end of the package installation process, just removes data left by other steps
    @output: 0 = all fine, >0 = error!
    '''
    def __cleanup_package(self, data):
        # remove unpack dir
        shutil.rmtree(data['unpackdir'],True)
        try:
            os.rmdir(data['unpackdir'])
        except OSError:
            pass
        return 0

    def __remove_package_from_database(self):
        self.error_on_not_prepared()
        self.Entropy.clientDbconn.removePackage(self.infoDict['removeidpackage'])
        return 0

    def __clear_cache(self):
        self.Entropy.clear_dump_cache(etpCache['advisories'])
        self.Entropy.clear_dump_cache(etpCache['filter_satisfied_deps'])
        self.Entropy.clear_dump_cache(etpCache['depends_tree'])
        self.Entropy.clear_dump_cache(etpCache['check_package_update'])
        self.Entropy.clear_dump_cache(etpCache['dep_tree'])
        self.Entropy.clear_dump_cache(etpCache['dbMatch']+etpConst['clientdbid']+"/")
        self.Entropy.clear_dump_cache(etpCache['dbSearch']+etpConst['clientdbid']+"/")

        self.__update_available_cache()
        try:
            self.__update_world_cache()
        except:
            self.Entropy.clear_dump_cache(etpCache['world_update'])

    def __update_world_cache(self):
        if self.Entropy.xcache and (self.action in ("install","remove",)):
            wc_dir = os.path.dirname(os.path.join(etpConst['dumpstoragedir'],etpCache['world_update']))
            wc_filename = os.path.basename(etpCache['world_update'])
            wc_cache_files = [os.path.join(wc_dir,x) for x in os.listdir(wc_dir) if x.startswith(wc_filename)]
            for cache_file in wc_cache_files:

                try:
                    data = self.Entropy.dumpTools.loadobj(cache_file, completePath = True)
                    (update, remove, fine) = data['r']
                    empty_deps = data['empty_deps']
                except:
                    self.Entropy.clear_dump_cache(etpCache['world_update'])
                    return

                if empty_deps:
                    continue

                if self.action == "install":
                    if self.matched_atom in update:
                        update.remove(self.matched_atom)
                        self.Entropy.dumpTools.dumpobj(
                            cache_file,
                            {'r':(update, remove, fine),'empty_deps': empty_deps},
                            completePath = True
                        )
                else:
                    key, slot = self.Entropy.clientDbconn.retrieveKeySlot(self.infoDict['removeidpackage'])
                    matches = self.Entropy.atomMatch(key, matchSlot = slot, multiMatch = True, multiRepo = True)
                    if matches[1] != 0:
                        # hell why! better to rip all off
                        self.Entropy.clear_dump_cache(etpCache['world_update'])
                        return
                    taint = False
                    for match in matches[0]:
                        if match in update:
                            taint = True
                            update.remove(match)
                        if match in remove:
                            taint = True
                            remove.remove(match)
                    if taint:
                        self.Entropy.dumpTools.dumpobj(
                            cache_file,
                            {'r':(update, remove, fine),'empty_deps': empty_deps},
                            completePath = True
                        )

        elif (not self.Entropy.xcache) or (self.action in ("install",)):
            self.Entropy.clear_dump_cache(etpCache['world_update'])

    def __update_available_cache(self):

        # update world available cache
        if self.Entropy.xcache and (self.action in ("remove","install")):
            c_hash = self.Entropy.get_available_packages_chash(etpConst['branch'])
            disk_cache = self.Entropy.dumpTools.loadobj(etpCache['world_available'])
            if disk_cache != None:
                try:
                    if disk_cache['chash'] == c_hash:

                        # remove and old install
                        if self.infoDict['removeidpackage'] != -1:
                            taint = False
                            key = self.Entropy.entropyTools.dep_getkey(self.infoDict['removeatom'])
                            slot = self.infoDict['slot']
                            matches = self.Entropy.atomMatch(key, matchSlot = slot, multiRepo = True, multiMatch = True)
                            if matches[1] == 0:
                                for mymatch in matches[0]:
                                    if mymatch not in disk_cache['available']:
                                        disk_cache['available'].append(mymatch)
                                        taint = True
                            if taint:
                                mydata = {}
                                mylist = []
                                for myidpackage,myrepo in disk_cache['available']:
                                    mydbc = self.Entropy.openRepositoryDatabase(myrepo)
                                    mydata[mydbc.retrieveAtom(myidpackage)] = (myidpackage,myrepo)
                                mykeys = mydata.keys()
                                mykeys.sort()
                                for mykey in mykeys:
                                    mylist.append(mydata[mykey])
                                disk_cache['available'] = mylist

                        # install, doing here because matches[0] could contain self.matched_atoms
                        if self.matched_atom in disk_cache['available']:
                            disk_cache['available'].remove(self.matched_atom)

                        self.Entropy.dumpTools.dumpobj(etpCache['world_available'],disk_cache)

                except KeyError:
                    try:
                        self.Entropy.dumpTools.dumpobj(etpCache['world_available'],{})
                    except IOError:
                        pass
        elif not self.Entropy.xcache:
            self.Entropy.clear_dump_cache(etpCache['world_available'])


    '''
    @description: install unpacked files, update database and also update gentoo db if requested
    @output: 0 = all fine, >0 = error!
    '''
    def __install_package(self):

        # clear on-disk cache
        self.__clear_cache()

        self.Entropy.clientLog.log(
            ETP_LOGPRI_INFO,
            ETP_LOGLEVEL_NORMAL,
            "Installing package: %s" % (self.infoDict['atom'],)
        )

        # copy files over - install
        rc = self.__move_image_to_system()
        if rc != 0:
            return rc

        # inject into database
        mytxt = blue("%s: %s") % (_("Updating database"),red(self.infoDict['atom']),)
        self.Entropy.updateProgress(
            mytxt,
            importance = 1,
            type = "info",
            header = red("   ## ")
        )
        newidpackage = self._install_package_into_database()
        # newidpackage = self.Entropy.entropyTools.spawnFunction( self._install_package_into_database )
        # ^^ it hangs on live systems!

        # remove old files and gentoo stuff
        if (self.infoDict['removeidpackage'] != -1):
            # doing a diff removal
            self.Entropy.clientLog.log(
                ETP_LOGPRI_INFO,
                ETP_LOGLEVEL_NORMAL,
                "Remove old package: %s" % (self.infoDict['removeatom'],)
            )
            self.infoDict['removeidpackage'] = -1 # disabling database removal

            if etpConst['gentoo-compat']:
                self.Entropy.clientLog.log(
                    ETP_LOGPRI_INFO,
                    ETP_LOGLEVEL_NORMAL,
                    "Removing Entropy and Gentoo database entry for %s" % (self.infoDict['removeatom'],)
                )
            else:
                self.Entropy.clientLog.log(
                    ETP_LOGPRI_INFO,
                    ETP_LOGLEVEL_NORMAL,
                    "Removing Entropy (only) database entry for %s" % (self.infoDict['removeatom'],)
                )

            self.Entropy.updateProgress(
                                    blue(_("Cleaning old package files...")),
                                    importance = 1,
                                    type = "info",
                                    header = red("   ## ")
                                )
            self.__remove_package()

        rc = 0
        if etpConst['gentoo-compat']:
            self.Entropy.clientLog.log(
                ETP_LOGPRI_INFO,
                ETP_LOGLEVEL_NORMAL,
                "Installing new Gentoo database entry: %s" % (self.infoDict['atom'],)
            )
            rc = self._install_package_into_gentoo_database(newidpackage)

        return rc

    '''
    @description: inject the database information into the Gentoo database
    @output: 0 = all fine, !=0 = error!
    '''
    def _install_package_into_gentoo_database(self, newidpackage):

        # handle gentoo-compat
        try:
            Spm = self.Entropy.Spm()
        except:
            return -1 # no Portage support
        portDbDir = Spm.get_vdb_path()
        if os.path.isdir(portDbDir):
            # extract xpak from unpackDir+etpConst['packagecontentdir']+"/"+package
            key = self.infoDict['category']+"/"+self.infoDict['name']
            atomsfound = set()
            dbdirs = os.listdir(portDbDir)
            if self.infoDict['category'] in dbdirs:
                catdirs = os.listdir(portDbDir+"/"+self.infoDict['category'])
                dirsfound = set([self.infoDict['category']+"/"+x for x in catdirs if key == self.Entropy.entropyTools.dep_getkey(self.infoDict['category']+"/"+x)])
                atomsfound.update(dirsfound)

            ### REMOVE
            # parse slot and match and remove
            if atomsfound:
                pkgToRemove = ''
                for atom in atomsfound:
                    atomslot = Spm.get_package_slot(atom)
                    # get slot from gentoo db
                    if atomslot == self.infoDict['slot']:
                        pkgToRemove = atom
                        break
                if (pkgToRemove):
                    removePath = portDbDir+pkgToRemove
                    shutil.rmtree(removePath,True)
                    try:
                        os.rmdir(removePath)
                    except OSError:
                        pass
            del atomsfound

            # we now install it
            if ((self.infoDict['xpakstatus'] != None) and \
                    os.path.isdir( self.infoDict['xpakpath'] + "/" + etpConst['entropyxpakdatarelativepath'])) or \
                    self.infoDict['merge_from']:

                if self.infoDict['merge_from']:
                    copypath = self.infoDict['xpakdir']
                    if not os.path.isdir(copypath):
                        return 0
                else:
                    copypath = self.infoDict['xpakpath']+"/"+etpConst['entropyxpakdatarelativepath']

                if not os.path.isdir(portDbDir+self.infoDict['category']):
                    os.makedirs(portDbDir+self.infoDict['category'],0755)
                destination = portDbDir+self.infoDict['category']+"/"+self.infoDict['name']+"-"+self.infoDict['version']
                if os.path.isdir(destination):
                    shutil.rmtree(destination)

                shutil.copytree(copypath,destination)

                # test if /var/cache/edb/counter is fine
                if os.path.isfile(etpConst['edbcounter']):
                    try:
                        f = open(etpConst['edbcounter'],"r")
                        counter = int(f.readline().strip())
                        f.close()
                    except:
                        # need file recreation, parse gentoo tree
                        counter = Spm.refill_counter()
                else:
                    counter = Spm.refill_counter()

                # write new counter to file
                if os.path.isdir(destination):
                    counter += 1
                    f = open(destination+"/"+etpConst['spm']['xpak_entries']['counter'],"w")
                    f.write(str(counter))
                    f.flush()
                    f.close()
                    f = open(etpConst['edbcounter'],"w")
                    f.write(str(counter))
                    f.flush()
                    f.close()
                    # update counter inside clientDatabase
                    self.Entropy.clientDbconn.insertCounter(newidpackage,counter)
                else:
                    mytxt = brown(_("Cannot update Gentoo counter, destination %s does not exist.") % (destination,))
                    self.Entropy.updateProgress(
                        red("QA: ")+mytxt,
                        importance = 1,
                        type = "warning",
                        header = darkred("   ## ")
                    )

        return 0

    '''
    @description: injects package info into the installed packages database
    @output: 0 = all fine, >0 = error!
    '''
    def _install_package_into_database(self):

        # fetch info
        dbconn = self.Entropy.openRepositoryDatabase(self.infoDict['repository'])
        data = dbconn.getPackageData(self.infoDict['idpackage'])
        # open client db
        # always set data['injected'] to False
        # installed packages database SHOULD never have more than one package for scope (key+slot)
        data['injected'] = False
        data['counter'] = -1 # gentoo counter will be set in self._install_package_into_gentoo_database()

        idpk, rev, x = self.Entropy.clientDbconn.handlePackage(etpData = data, forcedRevision = data['revision'])
        del data

        # update datecreation
        ctime = self.Entropy.entropyTools.getCurrentUnixTime()
        self.Entropy.clientDbconn.setDateCreation(idpk, str(ctime))

        # add idpk to the installedtable
        self.Entropy.clientDbconn.removePackageFromInstalledTable(idpk)
        self.Entropy.clientDbconn.addPackageToInstalledTable(idpk,self.infoDict['repository'])
        # update dependstable
        self.Entropy.clientDbconn.regenerateDependsTable(output = False)

        return idpk

    def __fill_image_dir(self, mergeFrom, imageDir):

        dbconn = self.Entropy.openRepositoryDatabase(self.infoDict['repository'])
        package_content = dbconn.retrieveContent(self.infoDict['idpackage'], extended = True, formatted = True)
        contents = [x for x in package_content]
        contents.sort()

        # collect files
        for path in contents:
            # convert back to filesystem str
            encoded_path = path
            path = os.path.join(mergeFrom,encoded_path[1:])
            topath = os.path.join(imageDir,encoded_path[1:])
            path = path.encode('raw_unicode_escape')
            topath = topath.encode('raw_unicode_escape')

            try:
                exist = os.lstat(path)
            except OSError:
                continue # skip file
            ftype = package_content[encoded_path]
            if str(ftype) == '0': ftype = 'dir' # force match below, '0' means databases without ftype
            if 'dir' == ftype and \
                not stat.S_ISDIR(exist.st_mode) and \
                os.path.isdir(path): # workaround for directory symlink issues
                path = os.path.realpath(path)

            copystat = False
            # if our directory is a symlink instead, then copy the symlink
            if os.path.islink(path):
                tolink = os.readlink(path)
                if os.path.islink(topath):
                    os.remove(topath)
                os.symlink(tolink,topath)
            elif os.path.isdir(path):
                if not os.path.isdir(topath):
                    os.makedirs(topath)
                    copystat = True
            elif os.path.isfile(path):
                if os.path.isfile(topath):
                    os.remove(topath) # should never happen
                shutil.copy2(path,topath)
                copystat = True

            if copystat:
                user = os.stat(path)[stat.ST_UID]
                group = os.stat(path)[stat.ST_GID]
                os.chown(topath,user,group)
                shutil.copystat(path,topath)


    def __move_image_to_system(self):

        # load CONFIG_PROTECT and its mask
        protect = etpRepositories[self.infoDict['repository']]['configprotect']
        mask = etpRepositories[self.infoDict['repository']]['configprotectmask']

        # setup imageDir properly
        imageDir = self.infoDict['imagedir']
        # XXX Python 2.4 workaround
        if sys.version[:3] == "2.4":
            imageDir = imageDir.encode('raw_unicode_escape')
        # XXX Python 2.4 workaround

        # merge data into system
        for currentdir,subdirs,files in os.walk(imageDir):
            # create subdirs
            for subdir in subdirs:

                imagepathDir = currentdir + "/" + subdir
                rootdir = etpConst['systemroot']+imagepathDir[len(imageDir):]

                # handle broken symlinks
                if os.path.islink(rootdir) and not os.path.exists(rootdir):# broken symlink
                    os.remove(rootdir)

                # if our directory is a file on the live system
                elif os.path.isfile(rootdir): # really weird...!
                    self.Entropy.clientLog.log(
                        ETP_LOGPRI_INFO,
                        ETP_LOGLEVEL_NORMAL,
                        "WARNING!!! %s is a file when it should be a directory !! Removing in 20 seconds..." % (rootdir,)
                    )
                    mytxt = darkred(_("%s is a file when should be a directory !! Removing in 20 seconds...") % (rootdir,))
                    self.Entropy.updateProgress(
                        red("QA: ")+mytxt,
                        importance = 1,
                        type = "warning",
                        header = red(" !!! ")
                    )
                    self.Entropy.entropyTools.ebeep(10)
                    time.sleep(20)
                    os.remove(rootdir)

                # if our directory is a symlink instead, then copy the symlink
                if os.path.islink(imagepathDir) and not os.path.isdir(rootdir):
                    # for security we skip live items that are dirs
                    tolink = os.readlink(imagepathDir)
                    if os.path.islink(rootdir):
                        os.remove(rootdir)
                    os.symlink(tolink,rootdir)
                elif (not os.path.isdir(rootdir)) and (not os.access(rootdir,os.R_OK)):
                    try:
                        # we should really force a simple mkdir first of all
                        os.mkdir(rootdir)
                    except OSError:
                        os.makedirs(rootdir)

                if not os.path.islink(rootdir) and os.access(rootdir,os.W_OK):
                    # symlink don't need permissions, also until os.walk ends they might be broken
                    # XXX also, added os.access() check because there might be directories/files unwriteable
                    # what to do otherwise?
                    user = os.stat(imagepathDir)[stat.ST_UID]
                    group = os.stat(imagepathDir)[stat.ST_GID]
                    os.chown(rootdir,user,group)
                    shutil.copystat(imagepathDir,rootdir)

            for item in files:

                fromfile = currentdir+"/"+item
                tofile = etpConst['systemroot']+fromfile[len(imageDir):]
                fromfile_encoded = fromfile
                #tofile_encoded = tofile
                # redecode to bytestring

                # XXX Python 2.4 bug workaround
                # If Python 2.4, .encode fails
                if sys.version[:3] != "2.4":
                    fromfile = fromfile.encode('raw_unicode_escape')
                    tofile = tofile.encode('raw_unicode_escape')
                # XXX Python 2.4 bug workaround

                if etpConst['collisionprotect'] > 1:
                    todbfile = fromfile[len(imageDir):]
                    myrc = self._handle_install_collision_protect(tofile, todbfile)
                    if not myrc:
                        continue

                protected, tofile, do_continue = self._handle_config_protect(protect, mask, fromfile, tofile)
                if do_continue:
                    continue

                try:

                    if os.path.realpath(fromfile) == os.path.realpath(tofile) and os.path.islink(tofile):
                        # there is a serious issue here, better removing tofile, happened to someone:
                        try: # try to cope...
                            os.remove(tofile)
                        except:
                            pass

                    # if our file is a dir on the live system
                    if os.path.isdir(tofile) and not os.path.islink(tofile): # really weird...!
                        self.Entropy.clientLog.log(
                            ETP_LOGPRI_INFO,
                            ETP_LOGLEVEL_NORMAL,
                            "WARNING!!! %s is a directory when it should be a file !! Removing in 20 seconds..." % (tofile,)
                        )
                        mytxt = _("%s is a directory when it should be a file !! Removing in 20 seconds...") % (tofile,)
                        self.Entropy.updateProgress(
                            red("QA: ")+darkred(mytxt),
                            importance = 1,
                            type = "warning",
                            header = red(" !!! ")
                        )
                        self.Entropy.entropyTools.ebeep(10)
                        time.sleep(20)
                        try:
                            shutil.rmtree(tofile, True)
                            os.rmdir(tofile)
                        except:
                            pass
                        try: # if it was a link
                            os.remove(tofile)
                        except OSError:
                            pass

                    # this also handles symlinks
                    # XXX
                    # XXX moving file using the raw format like portage does
                    # XXX
                    shutil.move(fromfile_encoded,tofile)

                except IOError, e:
                    if e.errno == 2:
                        # better to pass away, sometimes gentoo packages are fucked up and contain broken things
                        pass
                    else:
                        rc = os.system("mv "+fromfile+" "+tofile)
                        if (rc != 0):
                            return 4
                if protected:
                    # add to disk cache
                    oldquiet = etpUi['quiet']
                    etpUi['quiet'] = True
                    self.Entropy.FileUpdates.add_to_cache(tofile)
                    etpUi['quiet'] = oldquiet

        return 0

    def _handle_config_protect(self, protect, mask, fromfile, tofile):

        protected = False
        tofile_before_protect = tofile
        do_continue = False

        try:

            for x in protect:
                x = x.encode('raw_unicode_escape')
                if tofile.startswith(x):
                    protected = True
                    break

            if protected: # check if perhaps, file is masked, so unprotected
                newmask = [x.encode('raw_unicode_escape') for x in mask]
                if tofile in newmask:
                    protected = False
                elif os.path.dirname(tofile) in newmask:
                    protected = False

            if not os.path.lexists(tofile):
                protected = False # file doesn't exist

            # check if it's a text file
            if (protected) and os.path.isfile(tofile):
                protected = self.Entropy.entropyTools.istextfile(tofile)
            else:
                protected = False # it's not a file

            # request new tofile then
            if protected:
                if tofile not in etpConst['configprotectskip']:
                    tofile, prot_status = self.Entropy.entropyTools.allocateMaskedFile(tofile, fromfile)
                    if not prot_status:
                        protected = False
                    else:
                        oldtofile = tofile
                        if oldtofile.find("._cfg") != -1:
                            oldtofile = os.path.dirname(oldtofile)+"/"+os.path.basename(oldtofile)[10:]
                        self.Entropy.clientLog.log(
                            ETP_LOGPRI_INFO,
                            ETP_LOGLEVEL_NORMAL,
                            "Protecting config file: %s" % (oldtofile,)
                        )
                        mytxt = red("%s: %s") % (_("Protecting config file"),oldtofile,)
                        self.Entropy.updateProgress(
                            mytxt,
                            importance = 1,
                            type = "warning",
                            header = darkred("   ## ")
                        )
                else:
                    self.Entropy.clientLog.log(
                        ETP_LOGPRI_INFO,
                        ETP_LOGLEVEL_NORMAL,
                        "Skipping config file installation, as stated in equo.conf: %s" % (tofile,)
                    )
                    mytxt = "%s: %s" % (_("Skipping file installation"),tofile,)
                    self.Entropy.updateProgress(
                        mytxt,
                        importance = 1,
                        type = "warning",
                        header = darkred("   ## ")
                    )
                    do_continue = True

        except Exception, e:
            self.Entropy.entropyTools.printTraceback()
            protected = False # safely revert to false
            tofile = tofile_before_protect
            mytxt = darkred("%s: %s") % (_("Cannot check CONFIG PROTECTION. Error"),e,)
            self.Entropy.updateProgress(
                red("QA: ")+mytxt,
                importance = 1,
                type = "warning",
                header = darkred("   ## ")
            )

        return protected, tofile, do_continue


    def _handle_install_collision_protect(self, tofile, todbfile):
        avail = self.Entropy.clientDbconn.isFileAvailable(todbfile, get_id = True)
        if (self.infoDict['removeidpackage'] not in avail) and avail:
            mytxt = darkred(_("Collision found during install for"))
            mytxt += " %s - %s" % (blue(tofile),darkred(_("cannot overwrite")),)
            self.Entropy.updateProgress(
                red("QA: ")+mytxt,
                importance = 1,
                type = "warning",
                header = darkred("   ## ")
            )
            self.Entropy.clientLog.log(
                ETP_LOGPRI_INFO,
                ETP_LOGLEVEL_NORMAL,
                "WARNING!!! Collision found during install for %s - cannot overwrite" % (tofile,)
            )
            return False
        return True


    def fetch_step(self):
        self.error_on_not_prepared()
        mytxt = "%s: %s" % (blue(_("Downloading archive")),red(os.path.basename(self.infoDict['download'])),)
        self.Entropy.updateProgress(
            mytxt,
            importance = 1,
            type = "info",
            header = red("   ## ")
        )
        rc = self.Entropy.fetch_file_on_mirrors(
            self.infoDict['repository'],
            self.infoDict['download'],
            self.infoDict['checksum'],
            self.infoDict['verified']
        )
        if rc != 0:
            mytxt = "%s. %s: %s" % (
                red(_("Package cannot be fetched. Try to update repositories and retry")),
                blue(_("Error")),
                rc,
            )
            self.Entropy.updateProgress(
                mytxt,
                importance = 1,
                type = "error",
                header = darkred("   ## ")
            )
            return rc
        return 0

    def vanished_step(self):
        self.Entropy.updateProgress(
            blue(_("Installed package in queue vanished, skipping.")),
            importance = 1,
            type = "info",
            header = red("   ## ")
        )
        return 0

    def checksum_step(self):
        self.error_on_not_prepared()
        rc = self.match_checksum()
        return rc

    def unpack_step(self):
        self.error_on_not_prepared()

        if not self.infoDict['merge_from']:
            mytxt = "%s: %s" % (blue(_("Unpacking package")),red(os.path.basename(self.infoDict['download'])),)
            self.Entropy.updateProgress(
                mytxt,
                importance = 1,
                type = "info",
                header = red("   ## ")
        )
        else:
            mytxt = "%s: %s" % (blue(_("Merging package")),red(os.path.basename(self.infoDict['atom'])),)
            self.Entropy.updateProgress(
                mytxt,
                importance = 1,
                type = "info",
                header = red("   ## ")
            )
        rc = self.__unpack_package()
        if rc != 0:
            if rc == 512:
                errormsg = "%s. %s. %s: 512" % (
                    red(_("You are running out of disk space")),
                    red(_("I bet, you're probably Michele")),
                    blue(_("Error")),
                )
            else:
                errormsg = "%s. %s. %s: %s" % (
                    red(_("An error occured while trying to unpack the package")),
                    red(_("Check if your system is healthy")),
                    blue(_("Error")),
                    rc,
                )
            self.Entropy.updateProgress(
                errormsg,
                importance = 1,
                type = "error",
                header = red("   ## ")
            )
        return rc

    def install_step(self):
        self.error_on_not_prepared()
        mytxt = "%s: %s" % (blue(_("Installing package")),red(self.infoDict['atom']),)
        self.Entropy.updateProgress(
            mytxt,
            importance = 1,
            type = "info",
            header = red("   ## ")
        )
        rc = self.__install_package()
        if rc != 0:
            mytxt = "%s. %s. %s: %s" % (
                red(_("An error occured while trying to install the package")),
                red(_("Check if your system is healthy")),
                blue(_("Error")),
                rc,
            )
            self.Entropy.updateProgress(
                mytxt,
                importance = 1,
                type = "error",
                header = red("   ## ")
            )
        return rc

    def remove_step(self):
        self.error_on_not_prepared()
        mytxt = "%s: %s" % (blue(_("Removing data")),red(self.infoDict['removeatom']),)
        self.Entropy.updateProgress(
            mytxt,
            importance = 1,
            type = "info",
            header = red("   ## ")
        )
        rc = self.__remove_package()
        if rc != 0:
            mytxt = "%s. %s. %s: %s" % (
                red(_("An error occured while trying to remove the package")),
                red(_("heck if you have enough disk space on your hard disk")),
                blue(_("Error")),
                rc,
            )
            self.Entropy.updateProgress(
                mytxt,
                importance = 1,
                type = "error",
                header = red("   ## ")
            )
        return rc

    def cleanup_step(self):
        self.error_on_not_prepared()
        mytxt = "%s: %s" % (blue(_("Cleaning")),red(self.infoDict['atom']),)
        self.Entropy.updateProgress(
            mytxt,
            importance = 1,
            type = "info",
            header = red("   ## ")
        )
        tdict = {}
        tdict['unpackdir'] = self.infoDict['unpackdir']
        task = self.Entropy.entropyTools.parallelTask(self.__cleanup_package, tdict)
        task.parallel_wait()
        task.start()
        # we don't care if cleanupPackage fails since it's not critical
        return 0

    def logmessages_step(self):
        for msg in self.infoDict['messages']:
            self.Entropy.clientLog.write(">>>  "+msg)
        return 0

    def messages_step(self):
        self.error_on_not_prepared()
        # get messages
        if self.infoDict['messages']:
            self.Entropy.clientLog.log(
                ETP_LOGPRI_INFO,
                ETP_LOGLEVEL_NORMAL,
                "Message from %s:" % (self.infoDict['atom'],)
            )
            mytxt = "%s:" % (darkgreen(_("Compilation messages")),)
            self.Entropy.updateProgress(
                mytxt,
                importance = 0,
                type = "warning",
                header = brown("   ## ")
            )
        for msg in self.infoDict['messages']:
            self.Entropy.clientLog.log(ETP_LOGPRI_INFO,ETP_LOGLEVEL_NORMAL,msg)
            self.Entropy.updateProgress(
                msg,
                importance = 0,
                type = "warning",
                header = brown("   ## ")
            )
        if self.infoDict['messages']:
            self.Entropy.clientLog.log(ETP_LOGPRI_INFO,ETP_LOGLEVEL_NORMAL,"End message.")

    def postinstall_step(self):
        self.error_on_not_prepared()
        pkgdata = self.infoDict['triggers'].get('install')
        if pkgdata:
            Trigger = self.Entropy.Triggers('postinstall',pkgdata)
            Trigger.prepare()
            Trigger.run()
            Trigger.kill()
            del Trigger
        del pkgdata
        return 0

    def preinstall_step(self):
        self.error_on_not_prepared()
        pkgdata = self.infoDict['triggers'].get('install')
        if pkgdata:

            Trigger = self.Entropy.Triggers('preinstall',pkgdata)
            Trigger.prepare()
            if (self.infoDict.get("diffremoval") != None): # diffremoval is true only when the remove action is triggered by installPackages()
                if self.infoDict['diffremoval']:
                    remdata = self.infoDict['triggers'].get('remove')
                    if remdata:
                        rTrigger = self.Entropy.Triggers('preremove',remdata)
                        rTrigger.prepare()
                        Trigger.triggers = Trigger.triggers - rTrigger.triggers
                        rTrigger.kill()
                        del rTrigger
                    del remdata
            Trigger.run()
            Trigger.kill()
            del Trigger

        del pkgdata
        return 0

    def preremove_step(self):
        self.error_on_not_prepared()
        remdata = self.infoDict['triggers'].get('remove')
        if remdata:
            Trigger = self.Entropy.Triggers('preremove',remdata)
            Trigger.prepare()
            Trigger.run()
            Trigger.kill()
            del Trigger
        del remdata
        return 0

    def postremove_step(self):
        self.error_on_not_prepared()
        remdata = self.infoDict['triggers'].get('remove')
        if remdata:

            Trigger = self.Entropy.Triggers('postremove',remdata)
            Trigger.prepare()
            if self.infoDict['diffremoval'] and (self.infoDict.get("atom") != None):
                # diffremoval is true only when the remove action is triggered by installPackages()
                pkgdata = self.infoDict['triggers'].get('install')
                if pkgdata:
                    iTrigger = self.Entropy.Triggers('postinstall',pkgdata)
                    iTrigger.prepare()
                    Trigger.triggers = Trigger.triggers - iTrigger.triggers
                    iTrigger.kill()
                    del iTrigger
                del pkgdata
            Trigger.run()
            Trigger.kill()
            del Trigger

        del remdata
        return 0

    def removeconflict_step(self):

        for idpackage in self.infoDict['conflicts']:
            if not self.Entropy.clientDbconn.isIDPackageAvailable(idpackage):
                continue
            Package = self.Entropy.Package()
            Package.prepare((idpackage,),"remove", self.infoDict['remove_metaopts'])
            rc = Package.run(xterm_header = self.xterm_title)
            Package.kill()
            if rc != 0:
                return rc

        return 0

    def run_stepper(self, xterm_header):
        if xterm_header == None:
            xterm_header = ""

        if self.infoDict.has_key('remove_installed_vanished'):
            self.xterm_title += ' Installed package vanished'
            self.Entropy.setTitle(self.xterm_title)
            rc = self.vanished_step()
            return rc

        rc = 0
        for step in self.infoDict['steps']:
            self.xterm_title = xterm_header

            if step == "fetch":
                mytxt = _("Fetching")
                self.xterm_title += ' %s: %s' % (mytxt,os.path.basename(self.infoDict['download']),)
                self.Entropy.setTitle(self.xterm_title)
                rc = self.fetch_step()

            elif step == "checksum":
                mytxt = _("Verifying")
                self.xterm_title += ' %s: %s' % (mytxt,os.path.basename(self.infoDict['download']),)
                self.Entropy.setTitle(self.xterm_title)
                rc = self.checksum_step()

            elif step == "unpack":
                if not self.infoDict['merge_from']:
                    mytxt = _("Unpacking")
                    self.xterm_title += ' %s: %s' % (mytxt,os.path.basename(self.infoDict['download']),)
                else:
                    mytxt = _("Merging")
                    self.xterm_title += ' %s: %s' % (mytxt,os.path.basename(self.infoDict['atom']),)
                self.Entropy.setTitle(self.xterm_title)
                rc = self.unpack_step()

            elif step == "remove_conflicts":
                rc = self.removeconflict_step()

            elif step == "install":
                mytxt = _("Installing")
                self.xterm_title += ' %s: %s' % (mytxt,self.infoDict['atom'],)
                self.Entropy.setTitle(self.xterm_title)
                rc = self.install_step()

            elif step == "remove":
                mytxt = _("Removing")
                self.xterm_title += ' %s: %s' % (mytxt,self.infoDict['removeatom'],)
                self.Entropy.setTitle(self.xterm_title)
                rc = self.remove_step()

            elif step == "showmessages":
                rc = self.messages_step()

            elif step == "logmessages":
                rc = self.logmessages_step()

            elif step == "cleanup":
                mytxt = _("Cleaning")
                self.xterm_title += ' %s: %s' % (mytxt,self.infoDict['atom'],)
                self.Entropy.setTitle(self.xterm_title)
                rc = self.cleanup_step()

            elif step == "postinstall":
                mytxt = _("Postinstall")
                self.xterm_title += ' %s: %s' % (mytxt,self.infoDict['atom'],)
                self.Entropy.setTitle(self.xterm_title)
                rc = self.postinstall_step()

            elif step == "preinstall":
                mytxt = _("Preinstall")
                self.xterm_title += ' %s: %s' % (mytxt,self.infoDict['atom'],)
                self.Entropy.setTitle(self.xterm_title)
                rc = self.preinstall_step()

            elif step == "preremove":
                mytxt = _("Preremove")
                self.xterm_title += ' %s: %s' % (mytxt,self.infoDict['removeatom'],)
                self.Entropy.setTitle(self.xterm_title)
                rc = self.preremove_step()

            elif step == "postremove":
                mytxt = _("Postremove")
                self.xterm_title += ' %s: %s' % (mytxt,self.infoDict['removeatom'],)
                self.Entropy.setTitle(self.xterm_title)
                rc = self.postremove_step()

            if rc != 0:
                break

        return rc


    '''
        @description: execute the requested steps
        @input xterm_header: purely optional
    '''
    def run(self, xterm_header = None):
        self.error_on_not_prepared()

        gave_up = self.Entropy.lock_check(self.Entropy._resources_run_check_lock)
        if gave_up:
            return 20

        locked = self.Entropy.application_lock_check()
        if locked:
            self.Entropy._resources_run_remove_lock()
            return 21

        # lock
        self.Entropy._resources_run_create_lock()

        try:
            rc = self.run_stepper(xterm_header)
        except:
            self.Entropy._resources_run_remove_lock()
            raise

        # remove lock
        self.Entropy._resources_run_remove_lock()

        if rc != 0:
            self.Entropy.updateProgress(
                blue(_("An error occured. Action aborted.")),
                importance = 2,
                type = "error",
                header = darkred("   ## ")
            )
        return rc

    '''
       Install/Removal process preparation function
       - will generate all the metadata needed to run the action steps, creating infoDict automatically
       @input matched_atom(tuple): is what is returned by EquoInstance.atomMatch:
            (idpackage,repoid):
            (2000,u'sabayonlinux.org')
            NOTE: in case of remove action, matched_atom must be:
            (idpackage,)
        @input action(string): is an action to take, which must be one in self.valid_actions
    '''
    def prepare(self, matched_atom, action, metaopts = {}):

        # clear masking reasons
        maskingReasonsStorage.clear()

        self.error_on_prepared()
        self.check_action_validity(action)

        self.action = action
        self.matched_atom = matched_atom
        self.metaopts = metaopts
        # generate metadata dictionary
        self.generate_metadata()

    def generate_metadata(self):
        self.error_on_prepared()
        self.check_action_validity(self.action)

        if self.action == "fetch":
            self.__generate_fetch_metadata()
        elif self.action == "remove":
            self.__generate_remove_metadata()
        elif self.action == "install":
            self.__generate_install_metadata()
        self.prepared = True

    def __generate_remove_metadata(self):

        self.infoDict.clear()
        idpackage = self.matched_atom[0]

        if not self.Entropy.clientDbconn.isIDPackageAvailable(idpackage):
            self.infoDict['remove_installed_vanished'] = True
            return 0

        self.infoDict['triggers'] = {}
        self.infoDict['removeatom'] = self.Entropy.clientDbconn.retrieveAtom(idpackage)
        self.infoDict['slot'] = self.Entropy.clientDbconn.retrieveSlot(idpackage)
        self.infoDict['versiontag'] = self.Entropy.clientDbconn.retrieveVersionTag(idpackage)
        self.infoDict['removeidpackage'] = idpackage
        self.infoDict['diffremoval'] = False
        removeConfig = False
        if self.metaopts.has_key('removeconfig'):
            removeConfig = self.metaopts.get('removeconfig')
        self.infoDict['removeconfig'] = removeConfig
        self.infoDict['removecontent'] = self.Entropy.clientDbconn.retrieveContent(idpackage)
        self.infoDict['triggers']['remove'] = self.Entropy.clientDbconn.getTriggerInfo(idpackage)
        self.infoDict['triggers']['remove']['removecontent'] = self.infoDict['removecontent']
        self.infoDict['steps'] = []
        self.infoDict['steps'].append("preremove")
        self.infoDict['steps'].append("remove")
        self.infoDict['steps'].append("postremove")

        return 0

    def __generate_install_metadata(self):
        self.infoDict.clear()

        idpackage = self.matched_atom[0]
        repository = self.matched_atom[1]
        self.infoDict['idpackage'] = idpackage
        self.infoDict['repository'] = repository
        # get package atom
        dbconn = self.Entropy.openRepositoryDatabase(repository)
        self.infoDict['triggers'] = {}
        self.infoDict['atom'] = dbconn.retrieveAtom(idpackage)
        self.infoDict['slot'] = dbconn.retrieveSlot(idpackage)
        self.infoDict['version'] = dbconn.retrieveVersion(idpackage)
        self.infoDict['versiontag'] = dbconn.retrieveVersionTag(idpackage)
        self.infoDict['revision'] = dbconn.retrieveRevision(idpackage)
        self.infoDict['category'] = dbconn.retrieveCategory(idpackage)
        self.infoDict['download'] = dbconn.retrieveDownloadURL(idpackage)
        self.infoDict['name'] = dbconn.retrieveName(idpackage)
        self.infoDict['messages'] = dbconn.retrieveMessages(idpackage)
        self.infoDict['checksum'] = dbconn.retrieveDigest(idpackage)
        self.infoDict['accept_license'] = dbconn.retrieveLicensedataKeys(idpackage)
        self.infoDict['conflicts'] = self.Entropy.get_match_conflicts(self.matched_atom)

        # fill action queue
        self.infoDict['removeidpackage'] = -1
        removeConfig = False
        if self.metaopts.has_key('removeconfig'):
            removeConfig = self.metaopts.get('removeconfig')

        self.infoDict['remove_metaopts'] = {
            'removeconfig': True,
        }
        if self.metaopts.has_key('remove_metaopts'):
            self.infoDict['remove_metaopts'] = self.metaopts.get('remove_metaopts')

        self.infoDict['merge_from'] = None
        mf = self.metaopts.get('merge_from')
        if mf != None:
            self.infoDict['merge_from'] = unicode(mf)
        self.infoDict['removeconfig'] = removeConfig

        self.infoDict['removeidpackage'] = self.Entropy.retrieveInstalledIdPackage(
                                                self.Entropy.entropyTools.dep_getkey(self.infoDict['atom']),
                                                self.infoDict['slot']
                                            )

        if self.infoDict['removeidpackage'] != -1:
            avail = self.Entropy.clientDbconn.isIDPackageAvailable(self.infoDict['removeidpackage'])
            if avail:
                self.infoDict['removeatom'] = self.Entropy.clientDbconn.retrieveAtom(self.infoDict['removeidpackage'])
            else:
                self.infoDict['removeidpackage'] = -1

        # smartpackage ?
        self.infoDict['smartpackage'] = False
        # set unpack dir and image dir
        if self.infoDict['repository'].endswith(etpConst['packagesext']):
            # do arch check
            compiled_arch = dbconn.retrieveDownloadURL(idpackage)
            if compiled_arch.find("/"+etpSys['arch']+"/") == -1:
                self.infoDict.clear()
                self.prepared = False
                return -1
            self.infoDict['smartpackage'] = etpRepositories[self.infoDict['repository']]['smartpackage']
            self.infoDict['pkgpath'] = etpRepositories[self.infoDict['repository']]['pkgpath']
        else:
            self.infoDict['pkgpath'] = etpConst['entropyworkdir']+"/"+self.infoDict['download']
        self.infoDict['unpackdir'] = etpConst['entropyunpackdir']+"/"+self.infoDict['download']
        self.infoDict['imagedir'] = etpConst['entropyunpackdir']+"/"+self.infoDict['download']+"/"+etpConst['entropyimagerelativepath']

        # gentoo xpak data
        if etpConst['gentoo-compat']:
            self.infoDict['xpakpath'] = etpConst['entropyunpackdir']+"/"+self.infoDict['download']+"/"+etpConst['entropyxpakrelativepath']
            if not self.infoDict['merge_from']:
                self.infoDict['xpakstatus'] = None
                self.infoDict['xpakdir'] = self.infoDict['xpakpath']+"/"+etpConst['entropyxpakdatarelativepath']
            else:
                self.infoDict['xpakstatus'] = True
                portdbdir = 'var/db/pkg' # XXX hard coded ?
                portdbdir = os.path.join(self.infoDict['merge_from'],portdbdir)
                portdbdir = os.path.join(portdbdir,self.infoDict['category'])
                portdbdir = os.path.join(portdbdir,self.infoDict['name']+"-"+self.infoDict['version'])
                self.infoDict['xpakdir'] = portdbdir

        # compare both versions and if they match, disable removeidpackage
        if self.infoDict['removeidpackage'] != -1:
            installedVer = self.Entropy.clientDbconn.retrieveVersion(self.infoDict['removeidpackage'])
            installedTag = self.Entropy.clientDbconn.retrieveVersionTag(self.infoDict['removeidpackage'])
            installedRev = self.Entropy.clientDbconn.retrieveRevision(self.infoDict['removeidpackage'])
            pkgcmp = self.Entropy.entropyTools.entropyCompareVersions(
                (
                    self.infoDict['version'],
                    self.infoDict['versiontag'],
                    self.infoDict['revision'],
                ),
                (
                    installedVer,
                    installedTag,
                    installedRev,
                )
            )
            if pkgcmp == 0:
                self.infoDict['removeidpackage'] = -1
            else:
                # differential remove list
                self.infoDict['diffremoval'] = True
                self.infoDict['removeatom'] = self.Entropy.clientDbconn.retrieveAtom(self.infoDict['removeidpackage'])
                self.infoDict['removecontent'] = self.Entropy.clientDbconn.contentDiff(
                        self.infoDict['removeidpackage'],
                        dbconn,
                        idpackage
                )
                self.infoDict['triggers']['remove'] = self.Entropy.clientDbconn.getTriggerInfo(
                        self.infoDict['removeidpackage']
                )
                self.infoDict['triggers']['remove']['removecontent'] = self.infoDict['removecontent']

        # set steps
        self.infoDict['steps'] = []
        if self.infoDict['conflicts']:
            self.infoDict['steps'].append("remove_conflicts")
        # install
        if (self.infoDict['removeidpackage'] != -1):
            self.infoDict['steps'].append("preremove")
        self.infoDict['steps'].append("unpack")
        self.infoDict['steps'].append("preinstall")
        self.infoDict['steps'].append("install")
        if (self.infoDict['removeidpackage'] != -1):
            self.infoDict['steps'].append("postremove")
        self.infoDict['steps'].append("postinstall")
        if not etpConst['gentoo-compat']: # otherwise gentoo triggers will show that
            self.infoDict['steps'].append("showmessages")
        else:
            self.infoDict['steps'].append("logmessages")
        self.infoDict['steps'].append("cleanup")

        self.infoDict['triggers']['install'] = dbconn.getTriggerInfo(idpackage)
        self.infoDict['triggers']['install']['accept_license'] = self.infoDict['accept_license']
        self.infoDict['triggers']['install']['unpackdir'] = self.infoDict['unpackdir']
        if etpConst['gentoo-compat']:
            #self.infoDict['triggers']['install']['xpakpath'] = self.infoDict['xpakpath']
            self.infoDict['triggers']['install']['xpakdir'] = self.infoDict['xpakdir']

        return 0


    def __generate_fetch_metadata(self):
        self.infoDict.clear()

        idpackage = self.matched_atom[0]
        repository = self.matched_atom[1]
        dochecksum = True
        if self.metaopts.has_key('dochecksum'):
            dochecksum = self.metaopts.get('dochecksum')
        self.infoDict['repository'] = repository
        self.infoDict['idpackage'] = idpackage
        dbconn = self.Entropy.openRepositoryDatabase(repository)
        self.infoDict['atom'] = dbconn.retrieveAtom(idpackage)
        self.infoDict['checksum'] = dbconn.retrieveDigest(idpackage)
        self.infoDict['download'] = dbconn.retrieveDownloadURL(idpackage)
        self.infoDict['verified'] = False
        self.infoDict['steps'] = []
        if not repository.endswith(etpConst['packagesext']):
            if self.Entropy.check_needed_package_download(self.infoDict['download'], None) < 0:
                self.infoDict['steps'].append("fetch")
            if dochecksum:
                self.infoDict['steps'].append("checksum")
        # if file exists, first checksum then fetch
        if os.path.isfile(os.path.join(etpConst['entropyworkdir'],self.infoDict['download'])):
            # check size first
            repo_size = dbconn.retrieveSize(idpackage)
            f = open(os.path.join(etpConst['entropyworkdir'],self.infoDict['download']),"r")
            f.seek(0,2)
            disk_size = f.tell()
            f.close()
            if repo_size == disk_size:
                self.infoDict['steps'].reverse()
        return 0

class FileUpdatesInterface:

    def __init__(self, EquoInstance = None):

        if EquoInstance == None:
            self.Entropy = TextInterface()
            import dumpTools
            self.Entropy.dumpTools = dumpTools
        else:
            if not isinstance(EquoInstance,EquoInterface):
                mytxt = _("A valid Equo instance or subclass is needed")
                raise exceptionTools.IncorrectParameter("IncorrectParameter: %s" % (mytxt,))
            self.Entropy = EquoInstance

        self.scandata = None

    def merge_file(self, key):
        self.scanfs(dcache = True)
        self.do_backup(key)
        if os.access(etpConst['systemroot'] + self.scandata[key]['source'], os.R_OK):
            shutil.move(
                etpConst['systemroot'] + self.scandata[key]['source'],
                etpConst['systemroot'] + self.scandata[key]['destination']
            )
        self.remove_from_cache(key)

    def remove_file(self, key):
        self.scanfs(dcache = True)
        try:
            os.remove(etpConst['systemroot'] + self.scandata[key]['source'])
        except OSError:
            pass
        self.remove_from_cache(key)

    def do_backup(self, key):
        self.scanfs(dcache = True)
        if etpConst['filesbackup'] and os.path.isfile(etpConst['systemroot']+self.scandata[key]['destination']):
            bcount = 0
            backupfile = etpConst['systemroot'] + \
                os.path.dirname(self.scandata[key]['destination']) + \
                "/._equo_backup." + unicode(bcount) + "_" + \
                os.path.basename(self.scandata[key]['destination'])
            while os.path.lexists(backupfile):
                bcount += 1
                backupfile = etpConst['systemroot'] + \
                os.path.dirname(self.scandata[key]['destination']) + \
                "/._equo_backup." + unicode(bcount) + "_" + \
                os.path.basename(self.scandata[key]['destination'])
            try:
                shutil.copy2(etpConst['systemroot'] + self.scandata[key]['destination'],backupfile)
            except IOError:
                pass

    '''
    @description: scan for files that need to be merged
    @output: dictionary using filename as key
    '''
    def scanfs(self, dcache = True):

        if dcache:

            if self.scandata != None:
                return self.scandata

            # can we load cache?
            try:
                z = self.load_cache()
                if z != None:
                    self.scandata = z
                    return self.scandata
            except:
                pass

        # open client database to fill etpConst['dbconfigprotect']
        scandata = {}
        counter = 0
        name_cache = set()
        for path in etpConst['dbconfigprotect']:
            # it's a file?
            scanfile = False
            if os.path.isfile(path):
                # find inside basename
                path = os.path.dirname(path)
                scanfile = True

            for currentdir,subdirs,files in os.walk(path):
                for item in files:

                    if scanfile:
                        if path != item:
                            continue

                    filepath = os.path.join(currentdir,item)
                    if item.startswith("._cfg"):

                        # further check then
                        number = item[5:9]
                        try:
                            int(number)
                        except ValueError:
                            continue # not a valid etc-update file
                        if item[9] != "_": # no valid format provided
                            continue

                        if filepath in name_cache:
                            continue # skip, already done
                        name_cache.add(filepath)

                        mydict = self.generate_dict(filepath)
                        if mydict['automerge']:
                            mytxt = _("Automerging file")
                            self.Entropy.updateProgress(
                                darkred("%s: %s") % (
                                    mytxt,
                                    darkgreen(etpConst['systemroot'] + mydict['source']),
                                ),
                                importance = 0,
                                type = "info"
                            )
                            if os.path.isfile(etpConst['systemroot']+mydict['source']):
                                try:
                                    shutil.move(    etpConst['systemroot']+mydict['source'],
                                                    etpConst['systemroot']+mydict['destination']
                                    )
                                except IOError, e:
                                    mytxt = "%s :: %s: %s. %s: %s" % (
                                        red(_("I/O Error")),
                                        red(_("Cannot automerge file")),
                                        brown(etpConst['systemroot'] + mydict['source']),
                                        blue("Error"),
                                        e,
                                    )
                                    self.Entropy.updateProgress(
                                        mytxt,
                                        importance = 1,
                                        type = "warning"
                                    )
                            continue
                        else:
                            counter += 1
                            scandata[counter] = mydict.copy()

                        try:
                            self.Entropy.updateProgress(
                                "("+blue(str(counter))+") " + red(" file: ") + \
                                os.path.dirname(filepath) + "/" + os.path.basename(filepath)[10:],
                                importance = 1,
                                type = "info"
                            )
                        except:
                            pass # possible encoding issues
        # store data
        try:
            self.Entropy.dumpTools.dumpobj(etpCache['configfiles'],scandata)
        except IOError:
            pass
        self.scandata = scandata.copy()
        return scandata

    def load_cache(self):
        mytxt = _("Cache is corrupted")
        try:
            sd = self.Entropy.dumpTools.loadobj(etpCache['configfiles'])
            # check for corruption?
            if isinstance(sd, dict):
                # quick test if data is reliable
                try:

                    taint = False
                    name_cache = set()
                    # scan data
                    for x in sd:
                        mysource = sd[x]['source']

                        # filter dupies
                        if mysource in name_cache:
                            sd.pop(x)
                            continue

                        if not os.path.isfile(etpConst['systemroot']+mysource):
                            taint = True
                        name_cache.add(mysource)

                    if taint:
                        raise exceptionTools.CacheCorruptionError("CacheCorruptionError: %s." % (mytxt,))
                    return sd

                except (KeyError,EOFError):
                    raise exceptionTools.CacheCorruptionError("CacheCorruptionError: %s." % (mytxt,))
            else:
                raise exceptionTools.CacheCorruptionError("CacheCorruptionError: %s." % (mytxt,))
        except:
            raise exceptionTools.CacheCorruptionError("CacheCorruptionError: %s." % (mytxt,))

    '''
    @description: prints information about config files that should be updated
    @attention: please be sure that filepath is properly formatted before using this function
    '''
    def add_to_cache(self, filepath):
        self.scanfs(dcache = True)
        keys = self.scandata.keys()
        try:
            for key in keys:
                if self.scandata[key]['source'] == filepath[len(etpConst['systemroot']):]:
                    del self.scandata[key]
        except:
            pass
        # get next counter
        if keys:
            keys.sort()
            index = keys[-1]
        else:
            index = 0
        index += 1
        mydata = self.generate_dict(filepath)
        self.scandata[index] = mydata.copy()
        self.Entropy.dumpTools.dumpobj(etpCache['configfiles'],self.scandata)

    def remove_from_cache(self, key):
        self.scanfs(dcache = True)
        try:
            del self.scandata[key]
        except:
            pass
        self.Entropy.dumpTools.dumpobj(etpCache['configfiles'],self.scandata)
        return self.scandata

    def generate_dict(self, filepath):

        item = os.path.basename(filepath)
        currentdir = os.path.dirname(filepath)
        tofile = item[10:]
        number = item[5:9]
        try:
            int(number)
        except:
            mytxt = _("Invalid config file number")
            raise exceptionTools.InvalidDataType("InvalidDataType: %s '0000->9999'." % (mytxt,))
        tofilepath = currentdir+"/"+tofile
        mydict = {}
        mydict['revision'] = number
        mydict['destination'] = tofilepath[len(etpConst['systemroot']):]
        mydict['source'] = filepath[len(etpConst['systemroot']):]
        mydict['automerge'] = False
        if not os.path.isfile(tofilepath):
            mydict['automerge'] = True
        if (not mydict['automerge']):
            # is it trivial?
            try:
                if not os.path.lexists(filepath): # if file does not even exist
                    return mydict
                if os.path.islink(filepath):
                    # if it's broken, skip diff and automerge
                    if not os.path.exists(filepath):
                        return mydict
                result = commands.getoutput('diff -Nua '+filepath+' '+tofilepath+' | grep "^[+-][^+-]" | grep -v \'# .Header:.*\'')
                if not result:
                    mydict['automerge'] = True
            except:
                pass
            # another test
            if (not mydict['automerge']):
                try:
                    if not os.path.lexists(filepath): # if file does not even exist
                        return mydict
                    if os.path.islink(filepath):
                        # if it's broken, skip diff and automerge
                        if not os.path.exists(filepath):
                            return mydict
                    result = os.system('diff -Bbua '+filepath+' '+tofilepath+' | egrep \'^[+-]\' | egrep -v \'^[+-][\t ]*#|^--- |^\+\+\+ \' | egrep -qv \'^[-+][\t ]*$\'')
                    if result == 1:
                        mydict['automerge'] = True
                except:
                    pass
        return mydict

#
# repository control class, that's it
#
class RepoInterface:

    import dumpTools
    import entropyTools
    def __init__(self, EquoInstance, reponames = [], forceUpdate = False, noEquoCheck = False, fetchSecurity = True):

        self.LockScanner = None
        if not isinstance(EquoInstance,EquoInterface):
            mytxt = _("A valid Equo instance or subclass is needed")
            raise exceptionTools.IncorrectParameter("IncorrectParameter: %s" % (mytxt,))

        self.Entropy = EquoInstance
        self.reponames = reponames
        self.forceUpdate = forceUpdate
        self.syncErrors = False
        self.dbupdated = False
        self.newEquo = False
        self.fetchSecurity = fetchSecurity
        self.noEquoCheck = noEquoCheck
        self.alreadyUpdated = 0
        self.notAvailable = 0
        self.valid_eapis = [1,2,3]
        self.reset_dbformat_eapi(None)
        self.eapi3_socket = None
        self.current_repository_got_locked = False

        # check etpRepositories
        if not etpRepositories:
            mytxt = _("No repositories specified in %s") % (etpConst['repositoriesconf'],)
            raise exceptionTools.MissingParameter("MissingParameter: %s" % (mytxt,))

        # Test network connectivity
        conntest = self.entropyTools.get_remote_data(etpConst['conntestlink'])
        if not conntest:
            mytxt = _("Cannot connect to %s") % (etpConst['conntestlink'],)
            raise exceptionTools.OnlineMirrorError("OnlineMirrorError: %s" % (mytxt,))

        if not self.reponames:
            for x in etpRepositories:
                self.reponames.append(x)

    def __del__(self):
        if self.LockScanner != None:
            self.LockScanner.kill()

    def check_eapi3_availability(self, repository):
        # get database url
        dburl = etpRepositories[repository]['plain_database']
        if dburl.startswith("file://"):
            return False
        try:
            dburl = dburl.split("/")[2]
        except IndexError:
            return False

        #XXX dburl = '127.0.0.1'
        port = etpRepositories[repository]['service_port']

        try:
            self.eapi3_socket = RepositorySocketClientInterface(
                self.Entropy, EntropyRepositorySocketClientCommands
            )
            self.eapi3_socket.connect(dburl, port)
        except exceptionTools.ConnectionError:
            self.eapi3_socket = None
            return False
        return True

    def reset_dbformat_eapi(self, repository):

        self.dbformat_eapi = 2
        if repository != None:
            eapi_avail = self.check_eapi3_availability(repository)
            if eapi_avail:
                self.dbformat_eapi = 3

        # FIXME, find a way to do that without needing sqlite3 exec.
        if not os.access("/usr/bin/sqlite3",os.X_OK) or self.entropyTools.islive():
            self.dbformat_eapi = 1
        else:
            import subprocess
            rc = subprocess.call("/usr/bin/sqlite3 -version &> /dev/null", shell = True)
            if rc != 0: self.dbformat_eapi = 1

        eapi_env = os.getenv("FORCE_EAPI")
        if eapi_env != None:
            try:
                myeapi = int(eapi_env)
            except (ValueError,TypeError,):
                return
            if myeapi in self.valid_eapis:
                self.dbformat_eapi = myeapi


    def __validate_repository_id(self, repoid):
        if repoid not in self.reponames:
            mytxt = _("Repository is not listed in self.reponames")
            raise exceptionTools.InvalidData("InvalidData: %s" % (mytxt,))

    def __validate_compression_method(self, repo):

        self.__validate_repository_id(repo)

        cmethod = etpConst['etpdatabasecompressclasses'].get(etpRepositories[repo]['dbcformat'])
        if cmethod == None:
            mytxt = _("Wrong database compression method")
            raise exceptionTools.InvalidDataType("InvalidDataType: %s" % (mytxt,))

        return cmethod

    def __ensure_repository_path(self, repo):

        self.__validate_repository_id(repo)

        # create dir if it doesn't exist
        if not os.path.isdir(etpRepositories[repo]['dbpath']):
            os.makedirs(etpRepositories[repo]['dbpath'],0775)

        const_setup_perms(etpConst['etpdatabaseclientdir'],etpConst['entropygid'])

    def __construct_paths(self, item, repo, cmethod):

        supported_items = (
            "db","rev","ck",
            "lock","mask","dbdump",
            "dbdumpck","lic_whitelist","make.conf",
            "package.mask","package.unmask","package.keywords",
            "package.use",
        )
        if item not in supported_items:
            mytxt = _("Supported items: %s") % (supported_items,)
            raise exceptionTools.InvalidData("InvalidData: %s" % (mytxt,))

        if item == "db":
            if cmethod == None:
                mytxt = _("For %s, cmethod can't be None") % (item,)
                raise exceptionTools.InvalidData("InvalidData: %s" % (mytxt,))
            url = etpRepositories[repo]['database'] + "/" + etpConst[cmethod[2]]
            filepath = etpRepositories[repo]['dbpath'] + "/" + etpConst[cmethod[2]]
        elif item == "dbdump":
            if cmethod == None:
                mytxt = _("For %s, cmethod can't be None") % (item,)
                raise exceptionTools.InvalidData("InvalidData: %s" % (mytxt,))
            url = etpRepositories[repo]['database'] +   "/" + etpConst[cmethod[3]]
            filepath = etpRepositories[repo]['dbpath'] + "/" + etpConst[cmethod[3]]
        elif item == "rev":
            url = etpRepositories[repo]['database'] + "/" + etpConst['etpdatabaserevisionfile']
            filepath = etpRepositories[repo]['dbpath'] + "/" + etpConst['etpdatabaserevisionfile']
        elif item == "ck":
            url = etpRepositories[repo]['database'] + "/" + etpConst['etpdatabasehashfile']
            filepath = etpRepositories[repo]['dbpath'] + "/" + etpConst['etpdatabasehashfile']
        elif item == "dbdumpck":
            if cmethod == None:
                raise exceptionTools.InvalidData("InvalidData: for db, cmethod can't be None")
            url = etpRepositories[repo]['database'] + "/" + etpConst[cmethod[4]]
            filepath = etpRepositories[repo]['dbpath'] + "/" + etpConst[cmethod[4]]
        elif item == "mask":
            url = etpRepositories[repo]['database'] + "/" + etpConst['etpdatabasemaskfile']
            filepath = etpRepositories[repo]['dbpath'] + "/" + etpConst['etpdatabasemaskfile']
        elif item == "make.conf":
            myfile = os.path.basename(etpConst['spm']['global_make_conf'])
            url = etpRepositories[repo]['database'] + "/" + myfile
            filepath = etpRepositories[repo]['dbpath'] + "/" + myfile
        elif item == "package.mask":
            myfile = os.path.basename(etpConst['spm']['global_package_mask'])
            url = etpRepositories[repo]['database'] + "/" + myfile
            filepath = etpRepositories[repo]['dbpath'] + "/" + myfile
        elif item == "package.unmask":
            myfile = os.path.basename(etpConst['spm']['global_package_unmask'])
            url = etpRepositories[repo]['database'] + "/" + myfile
            filepath = etpRepositories[repo]['dbpath'] + "/" + myfile
        elif item == "package.keywords":
            myfile = os.path.basename(etpConst['spm']['global_package_keywords'])
            url = etpRepositories[repo]['database'] + "/" + myfile
            filepath = etpRepositories[repo]['dbpath'] + "/" + myfile
        elif item == "package.use":
            myfile = os.path.basename(etpConst['spm']['global_package_use'])
            url = etpRepositories[repo]['database'] + "/" + myfile
            filepath = etpRepositories[repo]['dbpath'] + "/" + myfile
        elif item == "lic_whitelist":
            url = etpRepositories[repo]['database'] + "/" + etpConst['etpdatabaselicwhitelistfile']
            filepath = etpRepositories[repo]['dbpath'] + "/" + etpConst['etpdatabaselicwhitelistfile']
        elif item == "lock":
            url = etpRepositories[repo]['database']+"/"+etpConst['etpdatabasedownloadlockfile']
            filepath = "/dev/null"

        return url, filepath

    def __remove_repository_files(self, repo, cmethod):

        dbfilenameid = cmethod[2]
        self.__validate_repository_id(repo)

        if self.dbformat_eapi == 1:
            if os.path.isfile(etpRepositories[repo]['dbpath']+"/"+etpConst['etpdatabasehashfile']):
                os.remove(etpRepositories[repo]['dbpath']+"/"+etpConst['etpdatabasehashfile'])
            if os.path.isfile(etpRepositories[repo]['dbpath']+"/"+etpConst[dbfilenameid]):
                os.remove(etpRepositories[repo]['dbpath']+"/"+etpConst[dbfilenameid])
            if os.path.isfile(etpRepositories[repo]['dbpath']+"/"+etpConst['etpdatabaserevisionfile']):
                os.remove(etpRepositories[repo]['dbpath']+"/"+etpConst['etpdatabaserevisionfile'])
        elif self.dbformat_eapi == 2:
            if os.path.isfile(etpRepositories[repo]['dbpath']+"/"+cmethod[4]):
                os.remove(etpRepositories[repo]['dbpath']+"/"+cmethod[4])
            if os.path.isfile(etpRepositories[repo]['dbpath']+"/"+etpConst[cmethod[3]]):
                os.remove(etpRepositories[repo]['dbpath']+"/"+etpConst[cmethod[3]])
            if os.path.isfile(etpRepositories[repo]['dbpath']+"/"+etpConst['etpdatabaserevisionfile']):
                os.remove(etpRepositories[repo]['dbpath']+"/"+etpConst['etpdatabaserevisionfile'])
        else:
            mytxt = _("self.dbformat_eapi must be in (1,2)")
            raise exceptionTools.InvalidData('InvalidData: %s' % (mytxt,))

    def __unpack_downloaded_database(self, repo, cmethod):

        self.__validate_repository_id(repo)
        rc = 0
        path = None

        if self.dbformat_eapi == 1:
            myfile = etpRepositories[repo]['dbpath']+"/"+etpConst[cmethod[2]]
            try:
                path = eval("self.entropyTools."+cmethod[1])(myfile)
            except EOFError:
                rc = 1
            if os.path.isfile(myfile):
                os.remove(myfile)
        elif self.dbformat_eapi == 2:
            myfile = etpRepositories[repo]['dbpath']+"/"+etpConst[cmethod[3]]
            try:
                path = eval("self.entropyTools."+cmethod[1])(myfile)
            except EOFError:
                rc = 1
            if os.path.isfile(myfile):
                os.remove(myfile)
        else:
            mytxt = _("self.dbformat_eapi must be in (1,2)")
            raise exceptionTools.InvalidData('InvalidData: %s' % (mytxt,))

        if rc == 0:
            self.Entropy.setup_default_file_perms(path)

        return rc

    def __verify_database_checksum(self, repo, cmethod = None):

        self.__validate_repository_id(repo)

        if self.dbformat_eapi == 1:
            dbfile = etpConst['etpdatabasefile']
            try:
                f = open(etpRepositories[repo]['dbpath']+"/"+etpConst['etpdatabasehashfile'],"r")
                md5hash = f.readline().strip()
                md5hash = md5hash.split()[0]
                f.close()
            except:
                return -1
        elif self.dbformat_eapi == 2:
            dbfile = etpConst[cmethod[3]]
            try:
                f = open(etpRepositories[repo]['dbpath']+"/"+etpConst[cmethod[4]],"r")
                md5hash = f.readline().strip()
                md5hash = md5hash.split()[0]
                f.close()
            except:
                return -1
        else:
            mytxt = _("self.dbformat_eapi must be in (1,2)")
            raise exceptionTools.InvalidData('InvalidData: %s' % (mytxt,))

        rc = self.entropyTools.compareMd5(etpRepositories[repo]['dbpath']+"/"+dbfile,md5hash)
        return rc

    # @returns -1 if the file is not available
    # @returns int>0 if the revision has been retrieved
    def get_online_repository_revision(self, repo):

        self.__validate_repository_id(repo)

        url = etpRepositories[repo]['database']+"/"+etpConst['etpdatabaserevisionfile']
        status = self.entropyTools.get_remote_data(url)
        if (status):
            status = status[0].strip()
            try:
                status = int(status)
            except ValueError:
                status = -1
            return status
        else:
            return -1

    def is_repository_updatable(self, repo):

        self.__validate_repository_id(repo)

        onlinestatus = self.get_online_repository_revision(repo)
        if (onlinestatus != -1):
            localstatus = self.Entropy.get_repository_revision(repo)
            if (localstatus == onlinestatus) and (not self.forceUpdate):
                return False
        else: # if == -1, means no repo found online
            return False
        return True

    def is_repository_unlocked(self, repo):

        self.__validate_repository_id(repo)

        rc = self.download_item("lock", repo)
        if rc: # cannot download database
            self.syncErrors = True
            return False
        return True

    def clear_repository_cache(self, repo):
        self.__validate_repository_id(repo)
        # idpackages are PRIMARY KEY AUTOINCREMENT, so
        # an idpackage won't be used more than once
        self.Entropy.clear_dump_cache(etpCache['dbMatch']+repo+"/")
        self.Entropy.clear_dump_cache(etpCache['dbSearch']+repo+"/")

    # this function can be reimplemented
    def download_item(self, item, repo, cmethod = None, lock_status_func = None):

        self.__validate_repository_id(repo)
        url, filepath = self.__construct_paths(item, repo, cmethod)

        # to avoid having permissions issues
        # it's better to remove the file before,
        # otherwise new permissions won't be written
        if os.path.isfile(filepath):
            os.remove(filepath)

        fetchConn = self.Entropy.urlFetcher(
            url,
            filepath,
            resume = False,
            abort_check_func = lock_status_func
        )
        fetchConn.progress = self.Entropy.progress

        rc = fetchConn.download()
        del fetchConn
        if rc in ("-1","-2","-3"):
            return False
        self.Entropy.setup_default_file_perms(filepath)
        return True

    def check_downloaded_database(self, repo, cmethod):
        dbfilename = etpConst['etpdatabasefile']
        if self.dbformat_eapi == 2:
            dbfilename = etpConst[cmethod[3]]
        # verify checksum
        mytxt = "%s %s %s" % (red(_("Checking downloaded database")),darkgreen(dbfilename),red("..."))
        self.Entropy.updateProgress(
            mytxt,
            importance = 0,
            back = True,
            type = "info",
            header = "\t"
        )
        db_status = self.__verify_database_checksum(repo, cmethod)
        if db_status == -1:
            mytxt = "%s. %s !" % (red(_("Cannot open digest")),red(_("Cannot verify database integrity")),)
            self.Entropy.updateProgress(
                mytxt,
                importance = 1,
                type = "warning",
                header = "\t"
            )
        elif db_status:
            mytxt = "%s: %s" % (red(_("Downloaded database status")),bold(_("OK")),)
            self.Entropy.updateProgress(
                mytxt,
                importance = 1,
                type = "info",
                header = "\t"
            )
        else:
            mytxt = "%s: %s" % (red(_("Downloaded database status")),darkred(_("ERROR")),)
            self.Entropy.updateProgress(
                mytxt,
                importance = 1,
                type = "error",
                header = "\t"
            )
            mytxt = "%s. %s" % (red(_("An error occured while checking database integrity")),red(_("Giving up")),)
            self.Entropy.updateProgress(
                mytxt,
                importance = 1,
                type = "error",
                header = "\t"
            )
            return 1
        return 0


    def show_repository_information(self, repo, count_info):

        self.Entropy.updateProgress(
            bold("%s") % ( etpRepositories[repo]['description'] ),
            importance = 2,
            type = "info",
            count = count_info,
            header = blue("  # ")
        )
        mytxt = "%s: %s" % (red(_("Database URL")),darkgreen(etpRepositories[repo]['database']),)
        self.Entropy.updateProgress(
            mytxt,
            importance = 1,
            type = "info",
            header = blue("  # ")
        )
        mytxt = "%s: %s" % (red(_("Database local path")),darkgreen(etpRepositories[repo]['dbpath']),)
        self.Entropy.updateProgress(
            mytxt,
            importance = 0,
            type = "info",
            header = blue("  # ")
        )
        mytxt = "%s: %s" % (red(_("Database EAPI")),darkgreen(str(self.dbformat_eapi)),)
        self.Entropy.updateProgress(
            mytxt,
            importance = 0,
            type = "info",
            header = blue("  # ")
        )

    def get_eapi3_local_database(self, repo):

        dbfile = os.path.join(etpRepositories[repo]['dbpath'],etpConst['etpdatabasefile'])
        mydbconn = None
        try:
            mydbconn = self.Entropy.openGenericDatabase(dbfile, xcache = False, indexing_override = False)
            mydbconn.validateDatabase()
        except (
            self.Entropy.dbapi2.OperationalError,
            self.Entropy.dbapi2.IntegrityError,
            exceptionTools.SystemDatabaseError,
            IOError,
            OSError,):
                mydbconn = None
        return mydbconn

    def get_eapi3_database_differences(self, repo, idpackages, compression, session):

        data = self.eapi3_socket.CmdInterface.differential_packages_comparison(
                idpackages,
                repo,
                etpConst['currentarch'],
                etpConst['product'],
                session_id = session,
                compression = compression
        )
        if isinstance(data,bool): # then it's probably == False
            return False,False,False
        elif not isinstance(data,dict):
            return None,None,None
        elif not data.has_key('added') or \
            not data.has_key('removed') or \
            not data.has_key('checksum'):
                return None,None,None
        return data['added'],data['removed'],data['checksum']

    def handle_eapi3_database_sync(self, repo, compression = False):

        session = self.eapi3_socket.open_session()
        mydbconn = self.get_eapi3_local_database(repo)
        if mydbconn == None:
            self.eapi3_socket.close_session(session)
            return False
        myidpackages = mydbconn.listAllIdpackages()

        added_ids, removed_ids, checksum = self.get_eapi3_database_differences(
            repo,
            myidpackages,
            compression,
            session
        )
        if None in (added_ids,removed_ids,checksum):
            mydbconn.closeDB()
            self.eapi3_socket.close_session(session)
            return False
        elif False in (added_ids,removed_ids,checksum):
            mydbconn.closeDB()
            self.eapi3_socket.close_session(session)
            mytxt = "%s: %s" % (
                blue(_("EAPI3 Service status")),
                darkred(_("remote database suddenly locked")),
            )
            self.Entropy.updateProgress(
                mytxt,
                importance = 0,
                type = "info",
                header = blue("  # "),
            )
            return None

        if not added_ids and not removed_ids and self.forceUpdate:
            mydbconn.closeDB()
            self.eapi3_socket.close_session(session)
            return False

        chunk_size = 8
        count = 0
        added_segments = []
        mytmp = set()

        for idpackage in added_ids:
            count += 1
            mytmp.add(idpackage)
            if count % chunk_size == 0:
                added_segments.append(list(mytmp))
                mytmp.clear()
        if mytmp:
            added_segments.append(list(mytmp))
        del mytmp

        # fetch and store
        count = 0
        maxcount = len(added_segments)
        for segment in added_segments:

            count += 1
            mytxt = "%s %s" % (blue(_("Fetching segments")), "...",)
            self.Entropy.updateProgress(
                mytxt,
                importance = 0,
                type = "info",
                header = "\t",
                back = True,
                count = (count,maxcount,)
            )
            pkgdata = self.eapi3_socket.CmdInterface.get_package_information(
                segment,
                repo,
                etpConst['currentarch'],
                etpConst['product'],
                session_id = session,
                compression = compression
            )
            if pkgdata == None:
                mytxt = "%s: %s" % (
                    blue(_("Fetch error on segment")),
                    darkred(str(segment)),
                )
                self.Entropy.updateProgress(
                    mytxt,
                    importance = 1,
                    type = "warning",
                    header = "\t",
                    count = (count,maxcount,)
                )
                mydbconn.closeDB()
                self.eapi3_socket.close_session(session)
                return False
            elif pkgdata == False:
                mytxt = "%s: %s" % (
                    blue(_("Service status")),
                    darkred("remote database suddenly locked"),
                )
                self.Entropy.updateProgress(
                    mytxt,
                    importance = 1,
                    type = "info",
                    header = "\t",
                    count = (count,maxcount,)
                )
                mydbconn.closeDB()
                self.eapi3_socket.close_session(session)
                return None
            for idpackage in pkgdata:
                self.dumpTools.dumpobj(
                    etpCache['eapi3_fetch']+str(idpackage),
                    pkgdata[idpackage]
                )
        del added_segments

        # now that we have all stored, add
        count = 0
        maxcount = len(added_ids)
        for idpackage in added_ids:
            count += 1
            mydata = self.dumpTools.loadobj(etpCache['eapi3_fetch']+str(idpackage))
            if mydata == None:
                mytxt = "%s: %s" % (
                    blue(_("Fetch error on segment while adding")),
                    darkred(str(segment)),
                )
                self.Entropy.updateProgress(
                    mytxt,
                    importance = 1,
                    type = "warning",
                    header = "\t",
                    count = (count,maxcount,)
                )
                mydbconn.closeDB()
                self.eapi3_socket.close_session(session)
                return False

            mytxt = "%s %s" % (blue(_("Injecting package")), darkgreen(mydata['atom']),)
            self.Entropy.updateProgress(
                mytxt,
                importance = 0,
                type = "info",
                header = "\t",
                back = True,
                count = (count,maxcount,)
            )
            mydbconn.addPackage(
                mydata,
                revision = mydata['revision'],
                idpackage = idpackage,
                do_remove = False,
                do_commit = False,
                formatted_content = True
            )

        self.Entropy.updateProgress(
            blue(_("Packages injection complete")),
            importance = 0,
            type = "info",
            header = "\t",
        )

        # now remove
        maxcount = len(removed_ids)
        count = 0
        for idpackage in removed_ids:
            myatom = mydbconn.retrieveAtom(idpackage)
            count += 1
            mytxt = "%s: %s" % (blue(_("Removing package")), darkred(str(myatom)),)
            self.Entropy.updateProgress(
                mytxt,
                importance = 0,
                type = "info",
                header = "\t",
                back = True,
                count = (count,maxcount,)
            )
            mydbconn.removePackage(idpackage, do_cleanup = False, do_commit = False)

        self.Entropy.updateProgress(
            blue(_("Packages removal complete")),
            importance = 0,
            type = "info",
            header = "\t",
        )

        #mydbconn.doCleanups()
        mydbconn.commitChanges()
        # now verify if both checksums match
        result = False
        mychecksum = mydbconn.database_checksum(do_order = True, strict = False)
        if checksum == mychecksum:
            result = True

        # bye!
        self.eapi3_socket.close_session(session)
        mydbconn.closeDB()
        return result

    def run_sync(self):

        self.dbupdated = False
        repocount = 0
        repolength = len(self.reponames)
        for repo in self.reponames:

            repocount += 1
            self.reset_dbformat_eapi(repo)
            self.show_repository_information(repo, (repocount,repolength))

            if not self.forceUpdate:
                updated = self.handle_repository_update(repo)
                if updated:
                    self.Entropy.cycleDone()
                    self.alreadyUpdated += 1
                    continue

            locked = self.handle_repository_lock(repo)
            if locked:
                self.notAvailable += 1
                self.Entropy.cycleDone()
                continue

            # clear database interface cache belonging to this repository
            self.clear_repository_cache(repo)
            self.__ensure_repository_path(repo)

            # dealing with EAPI
            # setting some vars
            do_skip = False
            skip_this_repo = False
            db_down_status = False
            do_db_update_transfer = False
            rc = 0
            # some variables
            dumpfile = os.path.join(etpRepositories[repo]['dbpath'],etpConst['etpdatabasedump'])
            dbfile = os.path.join(etpRepositories[repo]['dbpath'],etpConst['etpdatabasefile'])
            dbfile_old = dbfile+".sync"

            while 1:

                if do_skip:
                    break

                if self.dbformat_eapi < 3:

                    cmethod = self.__validate_compression_method(repo)
                    down_status = self.handle_database_download(repo, cmethod)
                    if not down_status:
                        self.Entropy.cycleDone()
                        self.notAvailable += 1
                        do_skip = True
                        skip_this_repo = True
                        continue
                    db_down_status = self.handle_database_checksum_download(repo, cmethod)

                    break

                elif self.dbformat_eapi == 3 and self.eapi3_socket == None:

                    self.dbformat_eapi -= 1
                    continue

                elif self.dbformat_eapi == 3 and not (os.path.isfile(dbfile) and os.access(dbfile,os.W_OK)):

                    do_db_update_transfer = None
                    self.dbformat_eapi -= 1
                    continue

                elif self.dbformat_eapi == 3:

                    status = self.handle_eapi3_database_sync(repo)
                    if status == False:
                        # set to none and completely skip database alignment
                        do_db_update_transfer = None
                        self.dbformat_eapi -= 1
                        continue
                    elif status == None: # remote db not available anymore ?
                        time.sleep(5)
                        locked = self.handle_repository_lock(repo)
                        if locked:
                            self.Entropy.cycleDone()
                            self.notAvailable += 1
                            do_skip = True
                        else: # ah, well... dunno then...
                            do_db_update_transfer = None
                            self.dbformat_eapi -= 1
                        continue

                    break

            if skip_this_repo:
                continue

            if self.dbformat_eapi in (1,2,):

                if self.dbformat_eapi == 2 and db_down_status:
                    rc = self.check_downloaded_database(repo, cmethod)
                    if rc != 0:
                        # delete all
                        self.__remove_repository_files(repo, cmethod)
                        self.syncErrors = True
                        self.Entropy.cycleDone()
                        continue

                if do_db_update_transfer == False:
                    if os.path.isfile(dbfile):
                        try:
                            shutil.move(dbfile,dbfile_old)
                            do_db_update_transfer = True
                        except:
                            pass

                # unpack database
                unpack_status = self.handle_downloaded_database_unpack(repo, cmethod)
                if not unpack_status:
                    # delete all
                    self.__remove_repository_files(repo, cmethod)
                    self.syncErrors = True
                    self.Entropy.cycleDone()
                    continue

                if self.dbformat_eapi == 1 and db_down_status:
                    rc = self.check_downloaded_database(repo, cmethod)
                    if rc != 0:
                        # delete all
                        self.__remove_repository_files(repo, cmethod)
                        self.syncErrors = True
                        self.Entropy.cycleDone()
                        if do_db_update_transfer:
                            try:
                                os.remove(dbfile_old)
                            except OSError:
                                pass
                        continue

                # re-validate
                if not os.path.isfile(dbfile):
                    do_db_update_transfer = False
                elif os.path.isfile(dbfile) and not do_db_update_transfer:
                    os.remove(dbfile)

                if self.dbformat_eapi == 2:
                    rc = self.do_eapi2_inject_downloaded_dump(dumpfile, dbfile, cmethod)

                if do_db_update_transfer:
                    self.do_eapi1_eapi2_databases_alignment(dbfile, dbfile_old)
                if self.dbformat_eapi == 2:
                    # remove the dump
                    os.remove(dumpfile)

            if rc != 0:
                # delete all
                self.__remove_repository_files(repo, cmethod)
                self.syncErrors = True
                self.Entropy.cycleDone()
                continue

            if os.path.isfile(dbfile) and os.access(dbfile,os.W_OK):
                self.Entropy.setup_default_file_perms(dbfile)

            # database is going to be updated
            self.dbupdated = True
            self.do_standard_items_download(repo)

            self.Entropy.update_repository_revision(repo)
            if self.Entropy.indexing:
                self.do_database_indexing(repo)
            self.Entropy.cycleDone()

        # keep them closed
        self.Entropy.closeAllRepositoryDatabases()
        self.Entropy.validate_repositories()
        self.Entropy.closeAllRepositoryDatabases()

        # clean caches
        if self.dbupdated:
            self.Entropy.generate_cache(
                depcache = self.Entropy.xcache,
                configcache = False,
                client_purge = False,
                install_queue = False
            )
            if self.fetchSecurity:
                self.do_update_security_advisories()

        if self.syncErrors:
            self.Entropy.updateProgress(
                red(_("Something bad happened. Please have a look.")),
                importance = 1,
                type = "warning",
                header = darkred(" @@ ")
            )
            self.syncErrors = True
            self.Entropy._resources_run_remove_lock()
            return 128

        if not self.noEquoCheck:
            self.check_entropy_updates()

        return 0

    def check_entropy_updates(self):
        rc = False
        if not self.noEquoCheck:
            try:
                rc = self.Entropy.check_equo_updates()
            except:
                pass
        if rc:
            self.newEquo = True
            mytxt = "%s: %s. %s." % (
                bold("Equo/Entropy"),
                blue(_("a new release is available")),
                darkred(_("Mind to install it before any other package")),
            )
            self.Entropy.updateProgress(
                mytxt,
                importance = 1,
                type = "info",
                header = bold(" !!! ")
            )

    def handle_downloaded_database_unpack(self, repo, cmethod):

        file_to_unpack = etpConst['etpdatabasedump']
        if self.dbformat_eapi == 1:
            file_to_unpack = etpConst['etpdatabasefile']
        mytxt = "%s %s %s" % (red(_("Unpacking database to")),darkgreen(file_to_unpack),red("..."),)
        self.Entropy.updateProgress(
            mytxt,
            importance = 0,
            type = "info",
            header = "\t"
        )

        myrc = self.__unpack_downloaded_database(repo, cmethod)
        if myrc != 0:
            mytxt = "%s %s !" % (red(_("Cannot unpack compressed package")),red(_("Skipping repository")),)
            self.Entropy.updateProgress(
                mytxt,
                importance = 1,
                type = "warning",
                header = "\t"
            )
            return False
        return True


    def handle_database_checksum_download(self, repo, cmethod):

        hashfile = etpConst['etpdatabasehashfile']
        downitem = 'ck'
        if self.dbformat_eapi == 2: # EAPI = 2
            hashfile = etpConst[cmethod[4]]
            downitem = 'dbdumpck'

        mytxt = "%s %s %s" % (red(_("Downloading checksum")),darkgreen(hashfile),red("..."),)
        # download checksum
        self.Entropy.updateProgress(
            mytxt,
            importance = 0,
            type = "info",
            header = "\t"
        )

        db_down_status = self.download_item(downitem, repo, cmethod)
        if not db_down_status:
            mytxt = "%s %s !" % (red(_("Cannot fetch checksum")),red(_("Cannot verify database integrity")),)
            self.Entropy.updateProgress(
                mytxt,
                importance = 1,
                type = "warning",
                header = "\t"
            )
        return db_down_status

    def load_background_repository_lock_check(self, repo):
        # kill previous
        self.current_repository_got_locked = False
        self.kill_previous_repository_lock_scanner()
        self.LockScanner = self.entropyTools.TimeScheduled( self.repository_lock_scanner, 2, {'repo': repo} )
        self.LockScanner.setName("Lock_Scanner::"+str(random.random()))
        self.LockScanner.start()

    def kill_previous_repository_lock_scanner(self):
        if self.LockScanner != None:
            self.LockScanner.kill()

    def repository_lock_scanner(self, data):
        repo = data['repo']
        locked = self.handle_repository_lock(repo)
        if locked:
            self.current_repository_got_locked = True

    def repository_lock_scanner_status(self):
        # raise an exception if repo got suddenly locked
        if self.current_repository_got_locked:
            mytxt = _("Current repository got suddenly locked. Download aborted.")
            raise exceptionTools.RepositoryError('RepositoryError %s' % (mytxt,))

    def handle_database_download(self, repo, cmethod):

        def show_repo_locked_message():
            mytxt = "%s: %s." % (bold(_("Attention")),red(_("remote database got suddenly locked")),)
            self.Entropy.updateProgress(
                mytxt,
                importance = 1,
                type = "warning",
                header = "\t"
            )

        # starting to download
        mytxt = "%s ..." % (red(_("Downloading repository database")),)
        self.Entropy.updateProgress(
            mytxt,
            importance = 1,
            type = "info",
            header = "\t"
        )

        down_status = False
        if self.dbformat_eapi == 2:
            # start a check in background
            self.load_background_repository_lock_check(repo)
            down_status = self.download_item("dbdump", repo, cmethod, lock_status_func = self.repository_lock_scanner_status)
            if self.current_repository_got_locked:
                self.kill_previous_repository_lock_scanner()
                show_repo_locked_message()
                return False
        if not down_status: # fallback to old db
            # start a check in background
            self.load_background_repository_lock_check(repo)
            self.dbformat_eapi = 1
            down_status = self.download_item("db", repo, cmethod, lock_status_func = self.repository_lock_scanner_status)
            if self.current_repository_got_locked:
                self.kill_previous_repository_lock_scanner()
                show_repo_locked_message()
                return False

        if not down_status:
            mytxt = "%s: %s." % (bold(_("Attention")),red(_("database does not exist online")),)
            self.Entropy.updateProgress(
                mytxt,
                importance = 1,
                type = "warning",
                header = "\t"
            )

        self.kill_previous_repository_lock_scanner()
        return down_status

    def handle_repository_update(self, repo):
        # check if database is already updated to the latest revision
        update = self.is_repository_updatable(repo)
        if not update:
            mytxt = "%s: %s." % (bold(_("Attention")),red(_("database is already up to date")),)
            self.Entropy.updateProgress(
                mytxt,
                importance = 1,
                type = "info",
                header = "\t"
            )
            return True
        return False

    def handle_repository_lock(self, repo):
        # get database lock
        unlocked = self.is_repository_unlocked(repo)
        if not unlocked:
            mytxt = "%s: %s. %s." % (
                bold(_("Attention")),
                red(_("Repository is being updated")),
                red(_("Try again in a few minutes")),
            )
            self.Entropy.updateProgress(
                mytxt,
                importance = 1,
                type = "warning",
                header = "\t"
            )
            return True
        return False

    def do_eapi1_eapi2_databases_alignment(self, dbfile, dbfile_old):

        dbconn = self.Entropy.openGenericDatabase(dbfile, xcache = False, indexing_override = False)
        old_dbconn = self.Entropy.openGenericDatabase(dbfile_old, xcache = False, indexing_override = False)
        upd_rc = 0
        try:
            upd_rc = old_dbconn.alignDatabases(dbconn, output_header = "\t")
        except (dbapi2.OperationalError,dbapi2.IntegrityError,):
            pass
        old_dbconn.closeDB()
        dbconn.closeDB()
        if upd_rc > 0:
            # -1 means no changes, == force used
            # 0 means too much hassle
            shutil.move(dbfile_old,dbfile)
        return upd_rc

    def do_eapi2_inject_downloaded_dump(self, dumpfile, dbfile, cmethod):

        # load the dump into database
        mytxt = "%s %s, %s %s" % (
            red(_("Injecting downloaded dump")),
            darkgreen(etpConst[cmethod[3]]),
            red(_("please wait")),
            red("..."),
        )
        self.Entropy.updateProgress(
            mytxt,
            importance = 0,
            type = "info",
            header = "\t"
        )
        dbconn = self.Entropy.openGenericDatabase(dbfile, xcache = False, indexing_override = False)
        rc = dbconn.doDatabaseImport(dumpfile, dbfile)
        dbconn.closeDB()
        return rc


    def do_update_security_advisories(self):
        # update Security Advisories
        try:
            securityConn = self.Entropy.Security()
            securityConn.fetch_advisories()
        except Exception, e:
            self.entropyTools.printTraceback()
            mytxt = "%s: %s" % (red(_("Advisories fetch error")),e,)
            self.Entropy.updateProgress(
                mytxt,
                importance = 1,
                type = "warning",
                header = darkred(" @@ ")
            )

    def do_standard_items_download(self, repo):

        download_items = [
            (
                "mask",
                etpConst['etpdatabasemaskfile'],
                True,
                "%s %s %s" % (
                    red(_("Downloading package mask")),
                    darkgreen(etpConst['etpdatabasemaskfile']),
                    red("..."),
                )
            ),
            (
                "lic_whitelist",
                etpConst['etpdatabaselicwhitelistfile'],
                True,
                "%s %s %s" % (
                    red(_("Downloading license whitelist")),
                    darkgreen(etpConst['etpdatabaselicwhitelistfile']),
                    red("..."),
                )
            ),
            (
                "rev",
                etpConst['etpdatabasemaskfile'],
                False,
                "%s %s %s" % (
                    red(_("Downloading revision")),
                    darkgreen(etpConst['etpdatabaserevisionfile']),
                    red("..."),
                )
            ),
            (
                "make.conf",
                os.path.basename(etpConst['spm']['global_make_conf']),
                True,
                "%s %s %s" % (
                    red(_("Downloading SPM global configuration")),
                    darkgreen(os.path.basename(etpConst['spm']['global_make_conf'])),
                    red("..."),
                )
            ),
            (
                "package.mask",
                os.path.basename(etpConst['spm']['global_package_mask']),
                True,
                "%s %s %s" % (
                    red(_("Downloading SPM package masking configuration")),
                    darkgreen(os.path.basename(etpConst['spm']['global_package_mask'])),
                    red("..."),
                )
            ),
            (
                "package.mask",
                os.path.basename(etpConst['spm']['global_package_unmask']),
                True,
                "%s %s %s" % (
                    red(_("Downloading SPM package unmasking configuration")),
                    darkgreen(os.path.basename(etpConst['spm']['global_package_unmask'])),
                    red("..."),
                )
            ),
            (
                "package.keywords",
                os.path.basename(etpConst['spm']['global_package_keywords']),
                True,
                "%s %s %s" % (
                    red(_("Downloading SPM package keywording configuration")),
                    darkgreen(os.path.basename(etpConst['spm']['global_package_keywords'])),
                    red("..."),
                )
            ),
            (
                "package.use",
                os.path.basename(etpConst['spm']['global_package_use']),
                True,
                "%s %s %s" % (
                    red(_("Downloading SPM package USE flags configuration")),
                    darkgreen(os.path.basename(etpConst['spm']['global_package_use'])),
                    red("..."),
                )
            ),
        ]

        for item, myfile, ignorable, mytxt in download_items:
            self.Entropy.updateProgress(
                mytxt,
                importance = 0,
                type = "info",
                header = "\t",
                back = True
            )
            mystatus = self.download_item(item, repo)
            mytype = 'info'
            if not mystatus:
                if ignorable:
                    message = "%s: %s." % (blue(myfile),red(_("not available, it's ok")))
                else:
                    mytype = 'warning'
                    message = "%s: %s." % (blue(myfile),darkred(_("not available, not much ok!")))
            else:
                message = "%s: %s." % (blue(myfile),red(_("not available, it's ok")))
            self.Entropy.updateProgress(
                message,
                importance = 0,
                type = mytype,
                header = "\t"
            )

        mytxt = "%s: %s" % (
            red(_("Repository revision")),
            bold(str(self.Entropy.get_repository_revision(repo))),
        )
        self.Entropy.updateProgress(
            mytxt,
            importance = 1,
            type = "info",
            header = "\t"
        )



    def do_database_indexing(self, repo):

        # renice a bit, to avoid eating resources
        old_prio = self.Entropy.set_priority(15)
        mytxt = red("%s ...") % (_("Indexing Repository metadata"),)
        self.Entropy.updateProgress(
            mytxt,
            importance = 1,
            type = "info",
            header = "\t",
            back = True
        )
        dbconn = self.Entropy.openRepositoryDatabase(repo)
        dbconn.createAllIndexes()
        # get list of indexes
        repo_indexes = dbconn.listAllIndexes()
        try: # client db can be absent
            client_indexes = self.Entropy.clientDbconn.listAllIndexes()
            if repo_indexes != client_indexes:
                self.Entropy.clientDbconn.createAllIndexes()
        except:
            pass
        self.Entropy.set_priority(old_prio)


    def sync(self):

        # close them
        self.Entropy.closeAllRepositoryDatabases()

        # let's dance!
        mytxt = darkgreen("%s ...") % (_("Repositories synchronization"),)
        self.Entropy.updateProgress(
            mytxt,
            importance = 2,
            type = "info",
            header = darkred(" @@ ")
        )

        gave_up = self.Entropy.lock_check(self.Entropy._resources_run_check_lock)
        if gave_up:
            return 3

        locked = self.Entropy.application_lock_check()
        if locked:
            self.Entropy._resources_run_remove_lock()
            return 4

        # lock
        self.Entropy._resources_run_create_lock()
        try:
            rc = self.run_sync()
        except:
            self.Entropy._resources_run_remove_lock()
            raise
        if rc: return rc

        # remove lock
        self.Entropy._resources_run_remove_lock()

        if (self.notAvailable >= len(self.reponames)):
            return 2
        elif (self.notAvailable > 0):
            return 1

        return 0

class QAInterface:

    import entropyTools
    def __init__(self, EntropyInterface):
        if not isinstance(EntropyInterface, (EquoInterface, ServerInterface)) and \
            not issubclass(EntropyInterface, (EquoInterface, ServerInterface)):
                mytxt = _("A valid EquoInterface/ServerInterface based instance is needed")
                raise exceptionTools.IncorrectParameter("IncorrectParameter: %s, (! %s !)" % (EntropyInterface,mytxt,))
        self.Entropy = EntropyInterface

    def test_depends_linking(self, idpackages, dbconn, repo = etpConst['officialrepositoryid']):

        scan_msg = blue(_("Now searching for broken depends"))
        self.Entropy.updateProgress(
            "[repo:%s] %s..." % (
                        darkgreen(repo),
                        scan_msg,
                    ),
            importance = 1,
            type = "info",
            header = red(" @@ ")
        )

        broken = False

        count = 0
        maxcount = len(idpackages)
        for idpackage in idpackages:
            count += 1
            atom = dbconn.retrieveAtom(idpackage)
            scan_msg = "%s, %s:" % (blue(_("scanning for broken depends")),darkgreen(atom),)
            self.Entropy.updateProgress(
                "[repo:%s] %s" % (
                    darkgreen(repo),
                    scan_msg,
                ),
                importance = 1,
                type = "info",
                header = blue(" @@ "),
                back = True,
                count = (count,maxcount,)
            )
            mydepends = dbconn.retrieveDepends(idpackage)
            if not mydepends:
                continue
            for mydepend in mydepends:
                myatom = dbconn.retrieveAtom(mydepend)
                self.Entropy.updateProgress(
                    "[repo:%s] %s => %s" % (
                        darkgreen(repo),
                        darkgreen(atom),
                        darkred(myatom),
                    ),
                    importance = 0,
                    type = "info",
                    header = blue(" @@ "),
                    back = True,
                    count = (count,maxcount,)
                )
                mycontent = dbconn.retrieveContent(mydepend)
                mybreakages = self.content_test(mycontent)
                if not mybreakages:
                    continue
                broken = True
                self.Entropy.updateProgress(
                    "[repo:%s] %s %s => %s" % (
                        darkgreen(repo),
                        darkgreen(atom),
                        darkred(myatom),
                        bold(_("broken libraries detected")),
                    ),
                    importance = 1,
                    type = "warning",
                    header = purple(" @@ "),
                    count = (count,maxcount,)
                )
                for mylib in mybreakages:
                    self.Entropy.updateProgress(
                        "%s %s:" % (
                            darkgreen(mylib),
                            red(_("needs")),
                        ),
                        importance = 1,
                        type = "warning",
                        header = brown("   ## ")
                    )
                    for needed in mybreakages[mylib]:
                        self.Entropy.updateProgress(
                            "%s" % (
                                red(needed),
                            ),
                            importance = 1,
                            type = "warning",
                            header = purple("     # ")
                        )
        return broken


    def scan_missing_dependencies(self, idpackages, dbconn, ask = True, self_check = False, repo = etpConst['officialrepositoryid']):

        taint = False
        scan_msg = blue(_("Now searching for missing RDEPENDs"))
        self.Entropy.updateProgress(
            "[repo:%s] %s..." % (
                        darkgreen(repo),
                        scan_msg,
                    ),
            importance = 1,
            type = "info",
            header = red(" @@ ")
        )
        scan_msg = blue(_("scanning for missing RDEPENDs"))
        count = 0
        maxcount = len(idpackages)
        for idpackage in idpackages:
            count += 1
            atom = dbconn.retrieveAtom(idpackage)
            self.Entropy.updateProgress(
                "[repo:%s] %s: %s" % (
                            darkgreen(repo),
                            scan_msg,
                            darkgreen(atom),
                        ),
                importance = 1,
                type = "info",
                header = blue(" @@ "),
                back = True,
                count = (count,maxcount,)
            )
            missing_extended, missing = self.get_missing_rdepends(dbconn, idpackage, self_check = self_check)
            if not missing:
                continue
            self.Entropy.updateProgress(
                "[repo:%s] %s: %s %s:" % (
                            darkgreen(repo),
                            blue("package"),
                            darkgreen(atom),
                            blue(_("is missing the following dependencies")),
                        ),
                importance = 1,
                type = "info",
                header = red(" @@ "),
                count = (count,maxcount,)
            )
            for missing_data in missing_extended:
                self.Entropy.updateProgress(
                        "%s:" % (brown(str(missing_data)),),
                        importance = 0,
                        type = "info",
                        header = purple("   ## ")
                )
                for dependency in missing_extended[missing_data]:
                    self.Entropy.updateProgress(
                            "%s" % (darkred(dependency),),
                            importance = 0,
                            type = "info",
                            header = blue("     # ")
                    )
            if ask:
                rc = self.Entropy.askQuestion(_("Do you want to add them?"))
                if rc == "No":
                    continue
                rc = self.Entropy.askQuestion(_("Selectively?"))
                if rc == "Yes":
                    newmissing = set()
                    for dependency in missing:
                        self.Entropy.updateProgress(
                            "[repo:%s|%s] %s" % (
                                    darkgreen(repo),
                                    brown(atom),
                                    blue(dependency),
                            ),
                            importance = 0,
                            type = "info",
                            header = blue(" @@ ")
                        )
                        rc = self.Entropy.askQuestion(_("Want to add?"))
                        if rc == "Yes":
                            newmissing.add(dependency)
                    missing = newmissing
            if missing:
                taint = True
                dbconn.insertDependencies(idpackage,missing)
                dbconn.commitChanges()
                self.Entropy.updateProgress(
                    "[repo:%s] %s: %s" % (
                                darkgreen(repo),
                                darkgreen(atom),
                                blue(_("missing dependencies added")),
                            ),
                    importance = 1,
                    type = "info",
                    header = red(" @@ "),
                    count = (count,maxcount,)
                )

        return taint

    def content_test(self, mycontent):

        def is_contained(needed, content):
            for item in content:
                if os.path.basename(item) == needed:
                    return True
            return False

        mylibs = {}
        for myfile in mycontent:
            myfile = myfile.encode('raw_unicode_escape')
            if not os.access(myfile,os.R_OK):
                continue
            if not os.path.isfile(myfile):
                continue
            if not self.entropyTools.is_elf_file(myfile):
                continue
            mylibs[myfile] = self.entropyTools.read_elf_dynamic_libraries(myfile)

        broken_libs = {}
        for mylib in mylibs:
            for myneeded in mylibs[mylib]:
                # is this inside myself ?
                if is_contained(myneeded, mycontent):
                    continue
                found = self.resolve_dynamic_library(myneeded, mylib)
                if found:
                    continue
                if not broken_libs.has_key(mylib):
                    broken_libs[mylib] = set()
                broken_libs[mylib].add(myneeded)

        return broken_libs

    def resolve_dynamic_library(self, library, requiring_executable):

        def do_resolve(mypaths):
            found_path = None
            for mypath in mypaths:
                mypath = os.path.join(etpConst['systemroot']+mypath,library)
                if not os.access(mypath,os.R_OK):
                    continue
                if os.path.isdir(mypath):
                    continue
                if not self.entropyTools.is_elf_file(mypath):
                    continue
                found_path = mypath
                break
            return found_path

        mypaths = self.entropyTools.collectLinkerPaths()
        found_path = do_resolve(mypaths)

        if not found_path:
            mypaths = self.entropyTools.read_elf_linker_paths(requiring_executable)
            found_path = do_resolve(mypaths)

        return found_path

    def get_missing_rdepends(self, dbconn, idpackage, self_check = False):

        rdepends = {}
        rdepends_plain = set()
        neededs = dbconn.retrieveNeeded(idpackage, extended = True)
        ldpaths = set(self.entropyTools.collectLinkerPaths())
        deps_content = set()
        dependencies = self._get_deep_dependency_list(dbconn, idpackage, atoms = True)
        dependencies_cache = set()

        def update_depscontent(mycontent, dbconn, ldpaths):
            return set( \
                    [   x for x in mycontent if os.path.dirname(x) in ldpaths \
                        and (dbconn.isNeededAvailable(os.path.basename(x)) > 0) ] \
                    )

        def is_in_content(myneeded, content):
            for item in content:
                item = os.path.basename(item)
                if myneeded == item:
                    return True
            return False

        for dependency in dependencies:
            match = dbconn.atomMatch(dependency)
            if match[0] != -1:
                mycontent = dbconn.retrieveContent(match[0])
                deps_content |= update_depscontent(mycontent, dbconn, ldpaths)
                key, slot = dbconn.retrieveKeySlot(match[0])
                dependencies_cache.add((key,slot))

        key, slot = dbconn.retrieveKeySlot(idpackage)
        mycontent = dbconn.retrieveContent(idpackage)
        deps_content |= update_depscontent(mycontent, dbconn, ldpaths)
        dependencies_cache.add((key,slot))

        idpackages_cache = set()
        for needed, elfclass in neededs:
            data_solved = dbconn.resolveNeeded(needed,elfclass)
            data_size = len(data_solved)
            data_solved = set([x for x in data_solved if x[0] not in idpackages_cache])
            if not data_solved or (data_size != len(data_solved)):
                continue

            if self_check:
                if is_in_content(needed,mycontent):
                    continue

            found = False
            for data in data_solved:
                if data[1] in deps_content:
                    found = True
                    break
            if not found:
                for data in data_solved:
                    key, slot = dbconn.retrieveKeySlot(data[0])
                    if (key,slot) not in dependencies_cache:
                        if not dbconn.isSystemPackage(data[0]):
                            if not rdepends.has_key((needed,elfclass)):
                                rdepends[(needed,elfclass)] = set()
                            rdepends[(needed,elfclass)].add(key+":"+slot)
                            rdepends_plain.add(key+":"+slot)
                        idpackages_cache.add(data[0])
        return rdepends, rdepends_plain

    def _get_deep_dependency_list(self, dbconn, idpackage, atoms = False):

        mybuffer = self.entropyTools.lifobuffer()
        matchcache = set()
        depcache = set()
        mydeps = dbconn.retrieveDependencies(idpackage)
        for mydep in mydeps:
            mybuffer.push(mydep)
        mydep = mybuffer.pop()

        while mydep != None:

            if mydep in depcache:
                mydep = mybuffer.pop()
                continue

            mymatch = dbconn.atomMatch(mydep)
            if atoms:
                matchcache.add(mydep)
            else:
                matchcache.add(mymatch[0])

            if mymatch[0] != -1:
                owndeps = dbconn.retrieveDependencies(mymatch[0])
                for owndep in owndeps:
                    mybuffer.push(owndep)

            depcache.add(mydep)
            mydep = mybuffer.pop()

        if atoms and -1 in matchcache:
            matchcache.remove(-1)

        return matchcache

'''
   Entropy FTP interface
'''
class FtpInterface:

    import ftplib
    import entropyTools
    import socket
    # this must be run before calling the other functions
    def __init__(self, ftpuri, EntropyInterface, verbose = True):

        if not isinstance(EntropyInterface, (EquoInterface, TextInterface, ServerInterface)) and \
            not issubclass(EntropyInterface, (EquoInterface, TextInterface, ServerInterface)):
                mytxt = _("A valid TextInterface based instance is needed")
                raise exceptionTools.IncorrectParameter("IncorrectParameter: %s, (! %s !)" % (EntropyInterface,mytxt,))

        self.Entropy = EntropyInterface
        self.verbose = verbose
        self.oldprogress = 0.0

        # import FTP modules
        self.socket.setdefaulttimeout(60)

        self.ftpuri = ftpuri
        self.ftphost = self.entropyTools.extractFTPHostFromUri(self.ftpuri)

        self.ftpuser = ftpuri.split("ftp://")[-1].split(":")[0]
        if (self.ftpuser == ""):
            self.ftpuser = "anonymous@"
            self.ftppassword = "anonymous"
        else:
            self.ftppassword = ftpuri.split("@")[:-1]
            if len(self.ftppassword) > 1:
                self.ftppassword = '@'.join(self.ftppassword)
                self.ftppassword = self.ftppassword.split(":")[-1]
                if (self.ftppassword == ""):
                    self.ftppassword = "anonymous"
            else:
                self.ftppassword = self.ftppassword[0]
                self.ftppassword = self.ftppassword.split(":")[-1]
                if (self.ftppassword == ""):
                    self.ftppassword = "anonymous"

        self.ftpport = ftpuri.split(":")[-1]
        try:
            self.ftpport = int(self.ftpport)
        except ValueError:
            self.ftpport = 21

        self.ftpdir = ftpuri.split("ftp://")[-1]
        self.ftpdir = self.ftpdir.split("/")[-1]
        self.ftpdir = self.ftpdir.split(":")[0]
        if self.ftpdir.endswith("/"):
            self.ftpdir = self.ftpdir[:len(self.ftpdir)-1]
        if not self.ftpdir:
            self.ftpdir = "/"

        count = 10
        while 1:
            count -= 1
            try:
                self.ftpconn = self.ftplib.FTP(self.ftphost)
                break
            except:
                if not count:
                    raise
                continue

        if self.verbose:
            mytxt = _("connecting with user")
            self.Entropy.updateProgress(
                "[ftp:%s] %s: %s" % (darkgreen(self.ftphost),mytxt,blue(self.ftpuser),),
                importance = 1,
                type = "info",
                header = darkgreen(" * ")
            )
        self.ftpconn.login(self.ftpuser,self.ftppassword)
        if self.verbose:
            mytxt = _("switching to")
            self.Entropy.updateProgress(
                "[ftp:%s] %s: %s" % (darkgreen(self.ftphost),mytxt,blue(self.ftpdir),),
                importance = 1,
                type = "info",
                header = darkgreen(" * ")
            )
        self.setCWD(self.ftpdir, dodir = True)

    def setBasedir(self):
        rc = self.setCWD(self.ftpdir)
        return rc

    # this can be used in case of exceptions
    def reconnectHost(self):
        # import FTP modules
        self.socket.setdefaulttimeout(60)
        counter = 10
        while 1:
            counter -= 1
            try:
                self.ftpconn = self.ftplib.FTP(self.ftphost)
                break
            except:
                if not counter:
                    raise
                continue
        if self.verbose:
            mytxt = _("reconnecting with user")
            self.Entropy.updateProgress(
                "[ftp:%s] %s: %s" % (darkgreen(self.ftphost),mytxt,blue(self.ftpuser),),
                importance = 1,
                type = "info",
                header = darkgreen(" * ")
            )
        self.ftpconn.login(self.ftpuser,self.ftppassword)
        if self.verbose:
            mytxt = _("switching to")
            self.Entropy.updateProgress(
                "[ftp:%s] %s: %s" % (darkgreen(self.ftphost),mytxt,blue(self.ftpdir),),
                importance = 1,
                type = "info",
                header = darkgreen(" * ")
            )
        self.setCWD(self.currentdir)

    def getHost(self):
        return self.ftphost

    def getPort(self):
        return self.ftpport

    def getDir(self):
        return self.ftpdir

    def getCWD(self):
        pwd = self.ftpconn.pwd()
        return pwd

    def setCWD(self, mydir, dodir = False):
        if self.verbose:
            mytxt = _("switching to")
            self.Entropy.updateProgress(
                "[ftp:%s] %s: %s" % (darkgreen(self.ftphost),mytxt,blue(mydir),),
                importance = 1,
                type = "info",
                header = darkgreen(" * ")
            )
        try:
            self.ftpconn.cwd(mydir)
        except self.ftplib.error_perm, e:
            if e[0][:3] == '550' and dodir:
                self.recursiveMkdir(mydir)
                self.ftpconn.cwd(mydir)
            else:
                raise
        self.currentdir = self.getCWD()

    def setPASV(self,bool):
        self.ftpconn.set_pasv(bool)

    def setChmod(self,chmodvalue,file):
        return self.ftpconn.voidcmd("SITE CHMOD "+str(chmodvalue)+" "+str(file))

    def getFileMtime(self,path):
        rc = self.ftpconn.sendcmd("mdtm "+path)
        return rc.split()[-1]

    def spawnCommand(self,cmd):
        return self.ftpconn.sendcmd(cmd)

    # list files and directory of a FTP
    # @returns a list
    def listDir(self):
        # directory is: self.ftpdir
        try:
            rc = self.ftpconn.nlst()
            _rc = []
            for i in rc:
                _rc.append(i.split("/")[-1])
            rc = _rc
        except:
            return []
        return rc

    # list if the file is available
    # @returns True or False
    def isFileAvailable(self,filename):
        # directory is: self.ftpdir
        try:
            rc = self.ftpconn.nlst()
            _rc = []
            for i in rc:
                _rc.append(i.split("/")[-1])
            rc = _rc
            for i in rc:
                if i == filename:
                    return True
            return False
        except:
            return False

    def deleteFile(self,file):
        try:
            rc = self.ftpconn.delete(file)
            if rc.startswith("250"):
                return True
            else:
                return False
        except:
            return False

    def recursiveMkdir(self, mypath):
        mydirs = [x for x in mypath.split("/") if x]
        mycurpath = ""
        for mydir in mydirs:
            mycurpath = os.path.join(mycurpath,mydir)
            if not self.isFileAvailable(mycurpath):
                try:
                    self.mkdir(mycurpath)
                except self.ftplib.error_perm, e:
                    if e[0][:3] != '550':
                        raise

    def mkdir(self,directory):
        return self.ftpconn.mkd(directory)

    # this function also supports callback, because storbinary doesn't
    def advancedStorBinary(self, cmd, fp, callback=None):
        ''' Store a file in binary mode. Our version supports a callback function'''
        self.ftpconn.voidcmd('TYPE I')
        conn = self.ftpconn.transfercmd(cmd)
        while 1:
            buf = fp.readline()
            if not buf: break
            conn.sendall(buf)
            if callback: callback(buf)
        conn.close()

        # that's another workaround
        #return "226"
        try:
            rc = self.ftpconn.voidresp()
        except:
            self.reconnectHost()
            return "226"
        return rc

    def updateProgress(self, buf_len):
        # get the buffer size
        self.mykByteCount += float(buf_len)/1024
        # create percentage
        if self.myFileSize < 1:
            myUploadPercentage = 100.0
        else:
            myUploadPercentage = round((round(self.mykByteCount,1)/self.myFileSize)*100,1)
        currentprogress = myUploadPercentage
        myUploadSize = round(self.mykByteCount,1)
        if (currentprogress > self.oldprogress+0.5) and (myUploadPercentage < 100.1) and (myUploadSize <= self.myFileSize):
            myUploadPercentage = str(myUploadPercentage)+"%"

            # create text
            mytxt = _("Upload status")
            currentText = brown("    <-> %s: " % (mytxt,)) + \
                green(str(myUploadSize)) + "/" + red(str(self.myFileSize)) + " kB " + \
                brown("[") + str(myUploadPercentage) + brown("]")
            print_info(currentText, back = True)
            # XXX too slow, reimplement self.updateProgress and do whatever you want
            #self.Entropy.updateProgress(currentText, importance = 0, type = "info", back = True)
            self.oldprogress = currentprogress

    def uploadFile(self,file,ascii = False):

        self.oldprogress = 0.0

        def uploadFileAndUpdateProgress(buf):
            self.updateProgress(len(buf))

        for i in range(10): # ten tries
            filename = file.split("/")[len(file.split("/"))-1]
            try:
                f = open(file,"r")
                # get file size
                self.myFileSize = round(float(os.stat(file)[6])/1024,1)
                self.mykByteCount = 0

                if self.isFileAvailable(filename+".tmp"):
                    self.deleteFile(filename+".tmp")

                if (ascii):
                    rc = self.ftpconn.storlines("STOR "+filename+".tmp",f)
                else:
                    rc = self.advancedStorBinary("STOR "+filename+".tmp", f, callback = uploadFileAndUpdateProgress )

                # now we can rename the file with its original name
                self.renameFile(filename+".tmp",filename)
                f.close()

                if rc.find("226") != -1: # upload complete
                    return True
                else:
                    return False

            except Exception, e: # connection reset by peer
                import traceback
                traceback.print_exc()
                self.Entropy.updateProgress(" ", importance = 0, type = "info")
                mytxt = red("%s: %s, %s... #%s") % (
                    _("Upload issue"),
                    e,
                    _("retrying"),
                    i+1,
                )
                self.Entropy.updateProgress(
                    mytxt,
                    importance = 1,
                    type = "warning",
                    header = "  "
                    )
                self.reconnectHost() # reconnect
                if self.isFileAvailable(filename):
                    self.deleteFile(filename)
                if self.isFileAvailable(filename+".tmp"):
                    self.deleteFile(filename+".tmp")
                pass

    def downloadFile(self, filename, downloaddir, ascii = False):

        self.oldprogress = 0.0

        def downloadFileStoreAndUpdateProgress(buf):
            # writing file buffer
            f.write(buf)
            # update progress
            self.mykByteCount += float(len(buf))/1024
            # create text
            cnt = round(self.mykByteCount,1)
            mytxt = _("Download status")
            currentText = brown("    <-> %s: " % (mytxt,)) + green(str(cnt)) + "/" + \
                red(str(self.myFileSize)) + " kB"
            self.Entropy.updateProgress(
                currentText,
                importance = 0,
                type = "info",
                back = True,
                count = (cnt, self.myFileSize),
                percent = True
            )

        # look if the file exist
        if self.isFileAvailable(filename):
            self.mykByteCount = 0
            # get the file size
            self.myFileSize = self.getFileSizeCompat(filename)
            if (self.myFileSize):
                self.myFileSize = round(float(int(self.myFileSize))/1024,1)
                if (self.myFileSize == 0):
                    self.myFileSize = 1
            else:
                self.myFileSize = 0
            if (not ascii):
                f = open(downloaddir+"/"+filename,"wb")
                rc = self.ftpconn.retrbinary('RETR '+filename, downloadFileStoreAndUpdateProgress, 1024)
            else:
                f = open(downloaddir+"/"+filename,"w")
                rc = self.ftpconn.retrlines('RETR '+filename, f.write)
            f.flush()
            f.close()
            if rc.find("226") != -1: # upload complete
                return True
            else:
                return False
        else:
            return None

    # also used to move files
    def renameFile(self,fromfile,tofile):
        rc = self.ftpconn.rename(fromfile,tofile)
        return rc

    def getFileSize(self,file):
        return self.ftpconn.size(file)

    def getFileSizeCompat(self,file):
        data = self.getRoughList()
        for item in data:
            if item.find(file) != -1:
                return item.split()[4]
        return ""

    def bufferizer(self,buf):
        self.FTPbuffer.append(buf)

    def getRoughList(self):
        self.FTPbuffer = []
        self.ftpconn.dir(self.bufferizer)
        return self.FTPbuffer

    def closeConnection(self):
        self.ftpconn.quit()


class urlFetcher:

    import entropyTools
    import socket
    def __init__(self, url, pathToSave, checksum = True, showSpeed = True, resume = True, abort_check_func = None):

        self.url = url
        self.resume = resume
        self.url = self.encodeUrl(self.url)
        self.pathToSave = pathToSave
        self.checksum = checksum
        self.showSpeed = showSpeed
        self.initVars()
        self.progress = None
        self.abort_check_func = abort_check_func
        self.user_agent = "Entropy/%s (compatible; %s; %s: %s %s %s)" % (
                                        etpConst['entropyversion'],
                                        "Entropy",
                                        os.path.basename(self.url),
                                        os.uname()[0],
                                        os.uname()[4],
                                        os.uname()[2],
        )
        self.extra_header_data = {}

        # resume support
        if os.path.isfile(self.pathToSave) and os.access(self.pathToSave,os.R_OK) and self.resume:
            self.localfile = open(self.pathToSave,"awb")
            self.localfile.seek(0,2)
            self.startingposition = int(self.localfile.tell())
            self.resumed = True
        else:
            self.localfile = open(self.pathToSave,"wb")

        # setup proxy, doing here because config is dynamic
        if etpConst['proxy']:
            proxy_support = urllib2.ProxyHandler(etpConst['proxy'])
            opener = urllib2.build_opener(proxy_support)
            urllib2.install_opener(opener)
        #FIXME else: unset opener??

    def encodeUrl(self, url):
        import urllib
        url = os.path.join(os.path.dirname(url),urllib.quote(os.path.basename(url)))
        return url

    def initVars(self):
        self.resumed = False
        self.bufferSize = 8192
        self.status = None
        self.remotefile = None
        self.downloadedsize = 0
        self.average = 0
        self.remotesize = 0
        self.oldaverage = 0.0
        # transfer status data
        self.startingposition = 0
        self.datatransfer = 0
        self.time_remaining = "(infinite)"
        self.elapsed = 0.0
        self.updatestep = 0.2
        self.speedlimit = etpConst['downloadspeedlimit'] # kbytes/sec
        self.transferpollingtime = float(1)/4

    def download(self):
        self.initVars()
        self.speedUpdater = self.entropyTools.TimeScheduled(
                    self.updateSpeedInfo,
                    self.transferpollingtime
        )
        self.speedUpdater.setName("download::"+self.url+str(random.random())) # set unique ID to thread, hopefully
        self.speedUpdater.start()

        # set timeout
        self.socket.setdefaulttimeout(20)


        if self.url.startswith("http://"):
            headers = { 'User-Agent' : self.user_agent }
            req = urllib2.Request(self.url, self.extra_header_data, headers)
        else:
            req = self.url

        # get file size if available
        try:
            self.remotefile = urllib2.urlopen(req)
        except KeyboardInterrupt:
            self.close()
            raise
        except:
            self.close()
            self.status = "-3"
            return self.status

        try:
            self.remotesize = int(self.remotefile.headers.get("content-length"))
            self.remotefile.close()
        except KeyboardInterrupt:
            self.close()
            raise
        except:
            pass

        # handle user stupidity
        try:
            request = self.url
            if ((self.startingposition > 0) and (self.remotesize > 0)) and (self.startingposition < self.remotesize):
                try:
                    request = urllib2.Request(
                        self.url,
                        headers = {
                            "Range" : "bytes=" + str(self.startingposition) + "-" + str(self.remotesize) 
                        }
                    )
                except KeyboardInterrupt:
                    self.close()
                    raise
                except:
                    pass
            elif (self.startingposition == self.remotesize):
                return self.prepare_return()
            else:
                self.localfile = open(self.pathToSave,"wb")
            self.remotefile = urllib2.urlopen(request)
        except KeyboardInterrupt:
            self.close()
            raise
        except:
            self.close()
            self.status = "-3"
            return self.status

        if self.remotesize > 0:
            self.remotesize = float(int(self.remotesize))/1024

        rsx = "x"
        while rsx != '':
            try:
                rsx = self.remotefile.read(self.bufferSize)
                if self.abort_check_func != None:
                    self.abort_check_func()
            except KeyboardInterrupt:
                self.close()
                raise
            except:
                # python 2.4 timeouts go here
                self.close()
                self.status = "-3"
                return self.status
            self.commitData(rsx)
            if self.showSpeed:
                self.updateProgress()
                self.oldaverage = self.average
            if self.speedlimit:
                while self.datatransfer > self.speedlimit*1024:
                    time.sleep(0.1)
                    if self.showSpeed:
                        self.updateProgress()
                        self.oldaverage = self.average

        # kill thread
        self.close()

        return self.prepare_return()


    def prepare_return(self):
        if self.checksum:
            self.status = self.entropyTools.md5sum(self.pathToSave)
            return self.status
        else:
            self.status = "-2"
            return self.status

    def commitData(self, mybuffer):
        # writing file buffer
        self.localfile.write(mybuffer)
        # update progress info
        self.downloadedsize = self.localfile.tell()
        kbytecount = float(self.downloadedsize)/1024
        self.average = int((kbytecount/self.remotesize)*100)

    def updateProgress(self):

        mytxt = _("Fetch")
        eta_txt = _("ETA")
        sec_txt = _("sec") # as in XX kb/sec

        currentText = darkred("    %s: " % (mytxt,)) + \
            darkgreen(str(round(float(self.downloadedsize)/1024,1))) + "/" + \
            red(str(round(self.remotesize,1))) + " kB"
        # create progress bar
        barsize = 10
        bartext = "["
        curbarsize = 1
        if self.average > self.oldaverage+self.updatestep:
            averagesize = (self.average*barsize)/100
            while averagesize > 0:
                curbarsize += 1
                bartext += "="
                averagesize -= 1
            bartext += ">"
            diffbarsize = barsize-curbarsize
            while diffbarsize > 0:
                bartext += " "
                diffbarsize -= 1
            if self.showSpeed:
                bartext += "] => %s" % (self.entropyTools.bytesIntoHuman(self.datatransfer),)
                bartext += "/%s : %s: %s" % (sec_txt,eta_txt,self.time_remaining,)
            else:
                bartext += "]"
            average = str(self.average)
            if len(average) < 2:
                average = " "+average
            currentText += " <->  "+average+"% "+bartext
            print_info(currentText,back = True)


    def close(self):
        try:
            self.localfile.flush()
            self.localfile.close()
        except:
            pass
        try:
            self.remotefile.close()
        except:
            pass
        self.speedUpdater.kill()
        self.socket.setdefaulttimeout(2)

    def updateSpeedInfo(self):
        self.elapsed += self.transferpollingtime
        # we have the diff size
        self.datatransfer = (self.downloadedsize-self.startingposition) / self.elapsed
        try:
            self.time_remaining = int(round((int(round(self.remotesize*1024,0))-int(round(self.downloadedsize,0)))/self.datatransfer,0))
            self.time_remaining = self.entropyTools.convertSecondsToFancyOutput(self.time_remaining)
        except:
            self.time_remaining = "(%s)" % (_("infinite"),)


class rssFeed:

    import entropyTools
    def __init__(self, filename, maxentries = 100):

        self.feed_title = etpConst['systemname']+" Online Repository Status"
        self.feed_title = self.feed_title.strip()
        self.feed_description = "Keep you updated on what's going on in the %s Repository." % (etpConst['systemname'],)
        self.feed_language = "en-EN"
        self.feed_editor = etpConst['rss-managing-editor']
        self.feed_copyright = "%s - (C) %s" % (
            etpConst['systemname'],
            self.entropyTools.getYear(),
        )

        self.file = filename
        self.items = {}
        self.itemscounter = 0
        self.maxentries = maxentries
        from xml.dom import minidom
        self.minidom = minidom

        # sanity check
        broken = False
        if os.path.isfile(self.file):
            try:
                self.xmldoc = self.minidom.parse(self.file)
            except:
                #time.sleep(5)
                broken = True

        if not os.path.isfile(self.file) or broken:
            self.title = self.feed_title
            self.description = self.feed_description
            self.language = self.feed_language
            self.cright = self.feed_copyright
            self.editor = self.feed_editor
            self.link = etpConst['rss-website-url']
            f = open(self.file,"w")
            f.write('')
            f.close()
        else:
            # parse file
            self.rssdoc = self.xmldoc.getElementsByTagName("rss")[0]
            self.channel = self.rssdoc.getElementsByTagName("channel")[0]
            self.title = self.channel.getElementsByTagName("title")[0].firstChild.data.strip()
            self.link = self.channel.getElementsByTagName("link")[0].firstChild.data.strip()
            self.description = self.channel.getElementsByTagName("description")[0].firstChild.data.strip()
            self.language = self.channel.getElementsByTagName("language")[0].firstChild.data.strip()
            self.cright = self.channel.getElementsByTagName("copyright")[0].firstChild.data.strip()
            self.editor = self.channel.getElementsByTagName("managingEditor")[0].firstChild.data.strip()
            entries = self.channel.getElementsByTagName("item")
            self.itemscounter = len(entries)
            if self.itemscounter > self.maxentries:
                self.itemscounter = self.maxentries
            mycounter = self.itemscounter
            for item in entries:
                if mycounter == 0: # max entries reached
                    break
                mycounter -= 1
                self.items[mycounter] = {}
                self.items[mycounter]['title'] = item.getElementsByTagName("title")[0].firstChild.data.strip()
                description = item.getElementsByTagName("description")[0].firstChild
                if description:
                    self.items[mycounter]['description'] = description.data.strip()
                else:
                    self.items[mycounter]['description'] = ""
                link = item.getElementsByTagName("link")[0].firstChild
                if link:
                    self.items[mycounter]['link'] = link.data.strip()
                else:
                    self.items[mycounter]['link'] = ""
                self.items[mycounter]['guid'] = item.getElementsByTagName("guid")[0].firstChild.data.strip()
                self.items[mycounter]['pubDate'] = item.getElementsByTagName("pubDate")[0].firstChild.data.strip()

    def addItem(self, title, link = '', description = ''):
        self.itemscounter += 1
        self.items[self.itemscounter] = {}
        self.items[self.itemscounter]['title'] = title
        self.items[self.itemscounter]['pubDate'] = time.strftime("%a, %d %b %Y %X +0000")
        self.items[self.itemscounter]['description'] = description
        self.items[self.itemscounter]['link'] = link
        if link:
            self.items[self.itemscounter]['guid'] = link
        else:
            myguid = etpConst['systemname'].lower()
            myguid = myguid.replace(" ","")
            self.items[self.itemscounter]['guid'] = myguid+"~"+description+str(self.itemscounter)
        return self.itemscounter

    def removeEntry(self, id):
        del self.items[id]
        self.itemscounter -= 1
        return len(self.itemscounter)

    def getEntries(self):
        return self.items, self.itemscounter

    def writeChanges(self):

        # filter entries to fit in maxentries
        if self.itemscounter > self.maxentries:
            tobefiltered = self.itemscounter - self.maxentries
            for index in range(tobefiltered):
                try:
                    del self.items[index]
                except KeyError:
                    pass

        doc = self.minidom.Document()

        rss = doc.createElement("rss")
        rss.setAttribute("version","2.0")
        rss.setAttribute("xmlns:atom","http://www.w3.org/2005/Atom")

        channel = doc.createElement("channel")

        # title
        title = doc.createElement("title")
        title_text = doc.createTextNode(unicode(self.title))
        title.appendChild(title_text)
        channel.appendChild(title)
        # link
        link = doc.createElement("link")
        link_text = doc.createTextNode(unicode(self.link))
        link.appendChild(link_text)
        channel.appendChild(link)
        # description
        description = doc.createElement("description")
        desc_text = doc.createTextNode(unicode(self.description))
        description.appendChild(desc_text)
        channel.appendChild(description)
        # language
        language = doc.createElement("language")
        lang_text = doc.createTextNode(unicode(self.language))
        language.appendChild(lang_text)
        channel.appendChild(language)
        # copyright
        cright = doc.createElement("copyright")
        cr_text = doc.createTextNode(unicode(self.cright))
        cright.appendChild(cr_text)
        channel.appendChild(cright)
        # managingEditor
        managingEditor = doc.createElement("managingEditor")
        ed_text = doc.createTextNode(unicode(self.editor))
        managingEditor.appendChild(ed_text)
        channel.appendChild(managingEditor)

        keys = self.items.keys()
        keys.reverse()
        for key in keys:

            # sanity check, you never know
            if not self.items.has_key(key):
                self.removeEntry(key)
                continue
            k_error = False
            for item in ['title','link','guid','description','pubDate']:
                if not self.items[key].has_key(item):
                    k_error = True
                    break
            if k_error:
                self.removeEntry(key)
                continue

            # item
            item = doc.createElement("item")
            # title
            item_title = doc.createElement("title")
            item_title_text = doc.createTextNode(unicode(self.items[key]['title']))
            item_title.appendChild(item_title_text)
            item.appendChild(item_title)
            # link
            item_link = doc.createElement("link")
            item_link_text = doc.createTextNode(unicode(self.items[key]['link']))
            item_link.appendChild(item_link_text)
            item.appendChild(item_link)
            # guid
            item_guid = doc.createElement("guid")
            item_guid.setAttribute("isPermaLink","true")
            item_guid_text = doc.createTextNode(unicode(self.items[key]['guid']))
            item_guid.appendChild(item_guid_text)
            item.appendChild(item_guid)
            # description
            item_desc = doc.createElement("description")
            item_desc_text = doc.createTextNode(unicode(self.items[key]['description']))
            item_desc.appendChild(item_desc_text)
            item.appendChild(item_desc)
            # pubdate
            item_date = doc.createElement("pubDate")
            item_date_text = doc.createTextNode(unicode(self.items[key]['pubDate']))
            item_date.appendChild(item_date_text)
            item.appendChild(item_date)

            # add item to channel
            channel.appendChild(item)

        # add channel to rss
        rss.appendChild(channel)
        doc.appendChild(rss)
        f = open(self.file,"w")
        f.writelines(doc.toprettyxml(indent="    "))
        f.flush()
        f.close()

class TriggerInterface:

    import entropyTools
    def __init__(self, EquoInstance, phase, pkgdata):

        if not isinstance(EquoInstance,EquoInterface):
            mytxt = _("A valid Entropy Instance is needed")
            raise exceptionTools.IncorrectParameter("IncorrectParameter: %s" % (mytxt,))

        self.Entropy = EquoInstance
        self.clientLog = self.Entropy.clientLog
        self.validPhases = ("preinstall","postinstall","preremove","postremove")
        self.pkgdata = pkgdata
        self.prepared = False
        self.triggers = set()
        self.gentoo_compat = etpConst['gentoo-compat']

        '''
        @ description: Gentoo toolchain variables
        '''
        self.MODULEDB_DIR="/var/lib/module-rebuild/"
        self.INITSERVICES_DIR="/var/lib/init.d/"

        ''' portage stuff '''
        if self.gentoo_compat:
            try:
                Spm = self.Entropy.Spm()
                self.Spm = Spm
            except Exception, e:
                self.entropyTools.printTraceback()
                mytxt = darkred("%s, %s: %s, %s !") % (
                    _("Portage interface can't be loaded"),
                    _("Error"),
                    e,
                    _("please fix"),
                )
                self.Entropy.updateProgress(
                    mytxt,
                    importance = 0,
                    header = bold(" !!! ")
                )
                self.gentoo_compat = False

        self.phase = phase
        # validate phase
        self.phaseValidation()

    def phaseValidation(self):
        if self.phase not in self.validPhases:
            mytxt = "%s: %s" % (_("Valid phases"),self.validPhases,)
            raise exceptionTools.InvalidData("InvalidData: %s" % (mytxt,))

    def prepare(self):
        self.triggers = eval("self."+self.phase)()
        remove = set()
        for trigger in self.triggers:
            if trigger in etpUi[self.phase+'_triggers_disable']:
                remove.add(trigger)
        self.triggers.difference_update(remove)
        del remove
        self.prepared = True

    def run(self):
        for trigger in self.triggers:
            eval("self.trigger_"+trigger)()

    def kill(self):
        self.prepared = False
        self.triggers.clear()

    def postinstall(self):

        functions = set()
        # Gentoo hook
        if self.gentoo_compat:
            functions.add('ebuild_postinstall')

        if self.pkgdata['trigger']:
            functions.add('call_ext_postinstall')

        # equo purge cache
        if self.pkgdata['category']+"/"+self.pkgdata['name'] == "sys-apps/entropy":
            functions.add("purgecache")

        # binutils configuration
        if self.pkgdata['category']+"/"+self.pkgdata['name'] == "sys-devel/binutils":
            functions.add("binutilsswitch")

        if (self.pkgdata['category']+"/"+self.pkgdata['name'] == "net-www/netscape-flash") and (etpSys['arch'] == "amd64"):
            functions.add("nspluginwrapper_fix_flash")

        # triggers that are not needed when gentoo-compat is enabled
        if not self.gentoo_compat:

            if "gnome2" in self.pkgdata['eclasses']:
                functions.add('iconscache')
                functions.add('gconfinstallschemas')
                functions.add('gconfreload')

            if self.pkgdata['name'] == "pygobject":
                functions.add('pygtksetup')

            # fonts configuration
            if self.pkgdata['category'] == "media-fonts":
                functions.add("fontconfig")

            # gcc configuration
            if self.pkgdata['category']+"/"+self.pkgdata['name'] == "sys-devel/gcc":
                functions.add("gccswitch")

            # kde package ?
            if "kde" in self.pkgdata['eclasses']:
                functions.add("kbuildsycoca")

            if "kde4-base" in self.pkgdata['eclasses'] or "kde4-meta" in self.pkgdata['eclasses']:
                functions.add("kbuildsycoca4")

            # update mime
            if "fdo-mime" in self.pkgdata['eclasses']:
                functions.add('mimeupdate')
                functions.add('mimedesktopupdate')

            if self.pkgdata['category']+"/"+self.pkgdata['name'] == "dev-db/sqlite":
                functions.add('sqliteinst')

            # python configuration
            if self.pkgdata['category']+"/"+self.pkgdata['name'] == "dev-lang/python":
                functions.add("pythoninst")

        # opengl configuration
        if (self.pkgdata['category'] == "x11-drivers") and (self.pkgdata['name'].startswith("nvidia-") or self.pkgdata['name'].startswith("ati-")):
            try:
                functions.remove("ebuild_postinstall") # disabling gentoo postinstall since we reimplemented it
            except:
                pass
            functions.add("openglsetup")

        # load linker paths
        ldpaths = self.Entropy.entropyTools.collectLinkerPaths()
        # prepare content
        for x in self.pkgdata['content']:
            if not self.gentoo_compat:
                if x.startswith("/usr/share/icons") and x.endswith("index.theme"):
                    functions.add('iconscache')
                if x.startswith("/usr/share/mime"):
                    functions.add('mimeupdate')
                if x.startswith("/usr/share/applications"):
                    functions.add('mimedesktopupdate')
                if x.startswith("/usr/share/omf"):
                    functions.add('scrollkeeper')
                if x.startswith("/etc/gconf/schemas"):
                    functions.add('gconfreload')
                if x == '/bin/su':
                    functions.add("susetuid")
                if x.startswith('/usr/share/java-config-2/vm/'):
                    functions.add('add_java_config_2')
            else:
                if x.startswith('/lib/modules/'):
                    try:
                        functions.remove("ebuild_postinstall")
                        # disabling gentoo postinstall since we reimplemented it
                    except:
                        pass
                    functions.add('kernelmod')
                if x.startswith('/boot/kernel-'):
                    functions.add('addbootablekernel')
                if x.startswith('/usr/src/'):
                    functions.add('createkernelsym')
                if x.startswith('/etc/env.d/'):
                    functions.add('env_update')
                if os.path.dirname(x) in ldpaths:
                    if x.find(".so") > -1:
                        functions.add('run_ldconfig')
        del ldpaths
        return functions

    def preinstall(self):

        functions = set()
        if self.pkgdata['trigger']:
            functions.add('call_ext_preinstall')

        # Gentoo hook
        if self.gentoo_compat:
            functions.add('ebuild_preinstall')

        for x in self.pkgdata['content']:
            if x.startswith("/etc/init.d/"):
                functions.add('initinform')
            if x.startswith("/boot"):
                functions.add('mountboot')
        return functions

    def postremove(self):

        functions = set()

        if self.pkgdata['trigger']:
            functions.add('call_ext_postremove')

        if not self.gentoo_compat:

            # kde package ?
            if "kde" in self.pkgdata['eclasses']:
                functions.add("kbuildsycoca")

            if "kde4-base" in self.pkgdata['eclasses'] or "kde4-meta" in self.pkgdata['eclasses']:
                functions.add("kbuildsycoca4")

            if self.pkgdata['name'] == "pygtk":
                functions.add('pygtkremove')

            if self.pkgdata['category']+"/"+self.pkgdata['name'] == "dev-db/sqlite":
                functions.add('sqliteinst')

            # python configuration
            if self.pkgdata['category']+"/"+self.pkgdata['name'] == "dev-lang/python":
                functions.add("pythoninst")

            # fonts configuration
            if self.pkgdata['category'] == "media-fonts":
                functions.add("fontconfig")

        # load linker paths
        ldpaths = self.Entropy.entropyTools.collectLinkerPaths()

        for x in self.pkgdata['removecontent']:
            if not self.gentoo_compat:
                if x.startswith("/usr/share/icons") and x.endswith("index.theme"):
                    functions.add('iconscache')
                if x.startswith("/usr/share/mime"):
                    functions.add('mimeupdate')
                if x.startswith("/usr/share/applications"):
                    functions.add('mimedesktopupdate')
                if x.startswith("/usr/share/omf"):
                    functions.add('scrollkeeper')
                if x.startswith("/etc/gconf/schemas"):
                    functions.add('gconfreload')
            else:
                if x.startswith('/boot/kernel-'):
                    functions.add('removebootablekernel')
                if x.startswith('/etc/init.d/'):
                    functions.add('removeinit')
                if x.endswith('.py'):
                    functions.add('cleanpy')
                if x.startswith('/etc/env.d/'):
                    functions.add('env_update')
                if os.path.dirname(x) in ldpaths:
                    if x.find(".so") > -1:
                        functions.add('run_ldconfig')
        del ldpaths
        return functions


    def preremove(self):

        functions = set()

        if self.pkgdata['trigger']:
            functions.add('call_ext_preremove')

        # Gentoo hook
        if self.gentoo_compat:
            functions.add('ebuild_preremove')
            functions.add('ebuild_postremove')
            # doing here because we need /var/db/pkg stuff in place and also because doesn't make any difference

        # opengl configuration
        if (self.pkgdata['category'] == "x11-drivers") and (self.pkgdata['name'].startswith("nvidia-") or self.pkgdata['name'].startswith("ati-")):
            try:
                functions.remove("ebuild_preremove")
                # disabling gentoo postinstall since we reimplemented it
                functions.remove("ebuild_postremove")
            except:
                pass
            functions.add("openglsetup_xorg")

        for x in self.pkgdata['removecontent']:
            if x.startswith("/etc/init.d/"):
                functions.add('initdisable')
            if x.startswith("/boot"):
                functions.add('mountboot')

        return functions


    '''
        Real triggers
    '''
    def trigger_call_ext_preinstall(self):
        rc = self.trigger_call_ext_generic()
        return rc

    def trigger_call_ext_postinstall(self):
        rc = self.trigger_call_ext_generic()
        return rc

    def trigger_call_ext_preremove(self):
        rc = self.trigger_call_ext_generic()
        return rc

    def trigger_call_ext_postremove(self):
        rc = self.trigger_call_ext_generic()
        return rc

    def trigger_call_ext_generic(self):

        # if mute, supress portage output
        if etpUi['mute']:
            oldsystderr = sys.stderr
            oldsysstdout = sys.stdout
            stdfile = open("/dev/null","w")
            sys.stdout = stdfile
            sys.stderr = stdfile

        triggerfile = etpConst['entropyunpackdir']+"/trigger-"+str(self.Entropy.entropyTools.getRandomNumber())
        while os.path.isfile(triggerfile):
            triggerfile = etpConst['entropyunpackdir']+"/trigger-"+str(self.Entropy.entropyTools.getRandomNumber())
        f = open(triggerfile,"w")
        for x in self.pkgdata['trigger']:
            f.write(x)
        f.close()

        # if mute, restore old stdout/stderr
        if etpUi['mute']:
            sys.stderr = oldsystderr
            sys.stdout = oldsysstdout
            stdfile.close()

        stage = self.phase
        pkgdata = self.pkgdata
        # since I am sick of seeing pychecker reporting this
        # let me do a nasty thing
        x = type(stage),type(pkgdata)
        del x
        my_ext_status = 0
        execfile(triggerfile)
        os.remove(triggerfile)
        return my_ext_status

    def trigger_nspluginwrapper_fix_flash(self):
        # check if nspluginwrapper is installed
        if os.access("/usr/bin/nspluginwrapper",os.X_OK):
            mytxt = "%s: nspluginwrapper flash plugin" % (_("Regenerating"),)
            self.Entropy.updateProgress(
                brown(mytxt),
                importance = 0,
                header = red("   ##")
            )
            quietstring = ''
            if etpUi['quiet']: quietstring = " &>/dev/null"
            cmds = [
                "nspluginwrapper -r /usr/lib64/nsbrowser/plugins/npwrapper.libflashplayer.so"+quietstring,
                "nspluginwrapper -i /usr/lib32/nsbrowser/plugins/libflashplayer.so"+quietstring
            ]
            if not etpConst['systemroot']:
                for cmd in cmds:
                    os.system(cmd)
            else:
                for cmd in cmds:
                    os.system('echo "'+cmd+'" | chroot '+etpConst['systemroot']+quietstring)

    def trigger_purgecache(self):
        self.Entropy.clientLog.log(
            ETP_LOGPRI_INFO,
            ETP_LOGLEVEL_NORMAL,
            "[POST] Purging Entropy cache..."
        )

        mytxt = "%s: %s." % (_("Please remember"),_("It is always better to leave Entropy updates isolated"),)
        self.Entropy.updateProgress(
            brown(mytxt),
            importance = 0,
            header = red("   ## ")
        )
        mytxt = "%s ..." % (_("Purging Entropy cache"),)
        self.Entropy.updateProgress(
            brown(mytxt),
            importance = 0,
            header = red("   ## ")
        )
        self.Entropy.purge_cache(False)

    def trigger_fontconfig(self):
        fontdirs = set()
        for xdir in self.pkgdata['content']:
            xdir = etpConst['systemroot']+xdir
            if xdir.startswith(etpConst['systemroot']+"/usr/share/fonts"):
                origdir = xdir[len(etpConst['systemroot'])+16:]
                if origdir:
                    if origdir.startswith("/"):
                        origdir = origdir.split("/")[1]
                        if os.path.isdir(xdir[:16]+"/"+origdir):
                            fontdirs.add(xdir[:16]+"/"+origdir)
        if fontdirs:
            self.Entropy.clientLog.log(
                ETP_LOGPRI_INFO,
                ETP_LOGLEVEL_NORMAL,
                "[POST] Configuring fonts directory..."
            )
            mytxt = "%s ..." % (_("Configuring fonts directories"),)
            self.Entropy.updateProgress(
                brown(mytxt),
                importance = 0,
                header = red("   ## ")
            )
        for fontdir in fontdirs:
            self.trigger_setup_font_dir(fontdir)
            self.trigger_setup_font_cache(fontdir)
        del fontdirs

    def trigger_gccswitch(self):
        self.Entropy.clientLog.log(
            ETP_LOGPRI_INFO,
            ETP_LOGLEVEL_NORMAL,
            "[POST] Configuring GCC Profile..."
        )
        mytxt = "%s ..." % (_("Configuring GCC Profile"),)
        self.Entropy.updateProgress(
            brown(mytxt),
            importance = 0,
            header = red("   ## ")
        )
        # get gcc profile
        pkgsplit = self.Entropy.entropyTools.catpkgsplit(
            self.pkgdata['category'] + "/" + self.pkgdata['name'] + "-" + self.pkgdata['version']
        )
        profile = self.pkgdata['chost']+"-"+pkgsplit[2]
        self.trigger_set_gcc_profile(profile)

    def trigger_iconscache(self):
        self.Entropy.clientLog.log(
            ETP_LOGPRI_INFO,
            ETP_LOGLEVEL_NORMAL,
            "[POST] Updating icons cache..."
        )
        mytxt = "%s ..." % (_("Updating icons cache"),)
        self.Entropy.updateProgress(
            brown(mytxt),
            importance = 0,
            header = red("   ## ")
        )
        for item in self.pkgdata['content']:
            item = etpConst['systemroot']+item
            if item.startswith(etpConst['systemroot']+"/usr/share/icons") and item.endswith("index.theme"):
                cachedir = os.path.dirname(item)
                self.trigger_generate_icons_cache(cachedir)

    def trigger_mimeupdate(self):
        self.Entropy.clientLog.log(
            ETP_LOGPRI_INFO,
            ETP_LOGLEVEL_NORMAL,
            "[POST] Updating shared mime info database..."
        )
        mytxt = "%s ..." % (_("Updating shared mime info database"),)
        self.Entropy.updateProgress(
            brown(mytxt),
            importance = 0,
            header = red("   ## ")
        )
        self.trigger_update_mime_db()

    def trigger_mimedesktopupdate(self):
        self.Entropy.clientLog.log(
            ETP_LOGPRI_INFO,
            ETP_LOGLEVEL_NORMAL,
            "[POST] Updating desktop mime database..."
        )
        mytxt = "%s ..." % (_("Updating desktop mime database"),)
        self.Entropy.updateProgress(
            brown(mytxt),
            importance = 0,
            header = red("   ## ")
        )
        self.trigger_update_mime_desktop_db()

    def trigger_scrollkeeper(self):
        self.Entropy.clientLog.log(
            ETP_LOGPRI_INFO,
            ETP_LOGLEVEL_NORMAL,
            "[POST] Updating scrollkeeper database..."
        )
        mytxt = "%s ..." % (_("Updating scrollkeeper database"),)
        self.Entropy.updateProgress(
            brown(mytxt),
            importance = 0,
            header = red("   ## ")
        )
        self.trigger_update_scrollkeeper_db()

    def trigger_gconfreload(self):
        self.Entropy.clientLog.log(
            ETP_LOGPRI_INFO,
            ETP_LOGLEVEL_NORMAL,
            "[POST] Reloading GConf2 database..."
        )
        mytxt = "%s ..." % (_("Reloading gconf2 database"),)
        self.Entropy.updateProgress(
            brown(mytxt),
            importance = 0,
            header = red("   ## ")
        )
        self.trigger_reload_gconf_db()

    def trigger_binutilsswitch(self):
        self.Entropy.clientLog.log(
            ETP_LOGPRI_INFO,
            ETP_LOGLEVEL_NORMAL,
            "[POST] Configuring Binutils Profile..."
        )
        mytxt = "%s ..." % (_("Configuring Binutils Profile"),)
        self.Entropy.updateProgress(
            brown(mytxt),
            importance = 0,
            header = red("   ## ")
        )
        # get binutils profile
        pkgsplit = self.Entropy.entropyTools.catpkgsplit(
            self.pkgdata['category'] + "/" + self.pkgdata['name'] + "-" + self.pkgdata['version']
        )
        profile = self.pkgdata['chost']+"-"+pkgsplit[2]
        self.trigger_set_binutils_profile(profile)

    def trigger_kernelmod(self):
        if self.pkgdata['category'] != "sys-kernel":
            self.Entropy.clientLog.log(
                ETP_LOGPRI_INFO,
                ETP_LOGLEVEL_NORMAL,
                "[POST] Updating moduledb..."
            )
            mytxt = "%s ..." % (_("Updating moduledb"),)
            self.Entropy.updateProgress(
                brown(mytxt),
                importance = 0,
                header = red("   ## ")
            )
            item = 'a:1:'+self.pkgdata['category']+"/"+self.pkgdata['name']+"-"+self.pkgdata['version']
            self.trigger_update_moduledb(item)
        mytxt = "%s ..." % (_("Running depmod"),)
        self.Entropy.updateProgress(
            brown(mytxt),
            importance = 0,
            header = red("   ## ")
        )
        # get kernel modules dir name
        name = ''
        for item in self.pkgdata['content']:
            item = etpConst['systemroot']+item
            if item.startswith(etpConst['systemroot']+"/lib/modules/"):
                name = item[len(etpConst['systemroot']):]
                name = name.split("/")[3]
                break
        if name:
            self.trigger_run_depmod(name)

    def trigger_pythoninst(self):
        self.Entropy.clientLog.log(
            ETP_LOGPRI_INFO,
            ETP_LOGLEVEL_NORMAL,
            "[POST] Configuring Python..."
        )
        mytxt = "%s ..." % (_("Configuring Python"),)
        self.Entropy.updateProgress(
            brown(mytxt),
            importance = 0,
            header = red("   ## ")
        )
        self.trigger_python_update_symlink()

    def trigger_sqliteinst(self):
        self.Entropy.clientLog.log(
            ETP_LOGPRI_INFO,
            ETP_LOGLEVEL_NORMAL,
            "[POST] Configuring SQLite..."
        )
        mytxt = "%s ..." % (_("Configuring SQLite"),)
        self.Entropy.updateProgress(
            brown(mytxt),
            importance = 0,
            header = red("   ## ")
        )
        self.trigger_sqlite_update_symlink()

    def trigger_initdisable(self):
        for item in self.pkgdata['removecontent']:
            item = etpConst['systemroot']+item
            if item.startswith(etpConst['systemroot']+"/etc/init.d/") and os.path.isfile(item):
                # running?
                #running = os.path.isfile(etpConst['systemroot']+self.INITSERVICES_DIR+'/started/'+os.path.basename(item))
                if not etpConst['systemroot']:
                    myroot = "/"
                else:
                    myroot = etpConst['systemroot']+"/"
                scheduled = not os.system('ROOT="'+myroot+'" rc-update show | grep '+os.path.basename(item)+'&> /dev/null')
                self.trigger_initdeactivate(item, scheduled)

    def trigger_initinform(self):
        for item in self.pkgdata['content']:
            item = etpConst['systemroot']+item
            if item.startswith(etpConst['systemroot']+"/etc/init.d/") and not os.path.isfile(etpConst['systemroot']+item):
                self.Entropy.clientLog.log(
                    ETP_LOGPRI_INFO,
                    ETP_LOGLEVEL_NORMAL,
                    "[PRE] A new service will be installed: %s" % (item,)
                )
                mytxt = "%s: %s" % (brown(_("A new service will be installed")),item,)
                self.Entropy.updateProgress(
                    mytxt,
                    importance = 0,
                    header = red("   ## ")
                )

    def trigger_removeinit(self):
        for item in self.pkgdata['removecontent']:
            item = etpConst['systemroot']+item
            if item.startswith(etpConst['systemroot']+"/etc/init.d/") and os.path.isfile(item):
                self.Entropy.clientLog.log(
                    ETP_LOGPRI_INFO,
                    ETP_LOGLEVEL_NORMAL,
                    "[POST] Removing boot service: %s" % (os.path.basename(item),)
                )
                mytxt = "%s: %s" % (brown(_("Removing boot service")),os.path.basename(item),)
                self.Entropy.updateProgress(
                    mytxt,
                    importance = 0,
                    header = red("   ## ")
                )
                if not etpConst['systemroot']:
                    myroot = "/"
                else:
                    myroot = etpConst['systemroot']+"/"
                try:
                    os.system('ROOT="'+myroot+'" rc-update del '+os.path.basename(item)+' &> /dev/null')
                except:
                    pass

    def trigger_openglsetup(self):
        opengl = "xorg-x11"
        if self.pkgdata['name'] == "nvidia-drivers":
            opengl = "nvidia"
        elif self.pkgdata['name'] == "ati-drivers":
            opengl = "ati"
        # is there eselect ?
        eselect = os.system("eselect opengl &> /dev/null")
        if eselect == 0:
            self.Entropy.clientLog.log(
                ETP_LOGPRI_INFO,
                ETP_LOGLEVEL_NORMAL,
                "[POST] Reconfiguring OpenGL to %s ..." % (opengl,)
            )
            mytxt = "%s ..." % (brown(_("Reconfiguring OpenGL")),)
            self.Entropy.updateProgress(
                mytxt,
                importance = 0,
                header = red("   ## ")
            )
            quietstring = ''
            if etpUi['quiet']: quietstring = " &>/dev/null"
            if etpConst['systemroot']:
                os.system('echo "eselect opengl set --use-old '+opengl+'" | chroot '+etpConst['systemroot']+quietstring)
            else:
                os.system('eselect opengl set --use-old '+opengl+quietstring)
        else:
            self.Entropy.clientLog.log(
                ETP_LOGPRI_INFO,
                ETP_LOGLEVEL_NORMAL,
                "[POST] Eselect NOT found, cannot run OpenGL trigger"
            )
            mytxt = "%s !" % (brown(_("Eselect NOT found, cannot run OpenGL trigger")),)
            self.Entropy.updateProgress(
                mytxt,
                importance = 0,
                header = red("   ##")
            )

    def trigger_openglsetup_xorg(self):
        eselect = os.system("eselect opengl &> /dev/null")
        if eselect == 0:
            self.Entropy.clientLog.log(
                ETP_LOGPRI_INFO,
                ETP_LOGLEVEL_NORMAL,
                "[POST] Reconfiguring OpenGL to fallback xorg-x11 ..."
            )
            mytxt = "%s ..." % (brown(_("Reconfiguring OpenGL")),)
            self.Entropy.updateProgress(
                mytxt,
                importance = 0,
                header = red("   ## ")
            )
            quietstring = ''
            if etpUi['quiet']: quietstring = " &>/dev/null"
            if etpConst['systemroot']:
                os.system('echo "eselect opengl set xorg-x11" | chroot '+etpConst['systemroot']+quietstring)
            else:
                os.system('eselect opengl set xorg-x11'+quietstring)
        else:
            self.Entropy.clientLog.log(
                ETP_LOGPRI_INFO,
                ETP_LOGLEVEL_NORMAL,
                "[POST] Eselect NOT found, cannot run OpenGL trigger"
            )
            mytxt = "%s !" % (brown(_("Eselect NOT found, cannot run OpenGL trigger")),)
            self.Entropy.updateProgress(
                mytxt,
                importance = 0,
                header = red("   ##")
            )

    # FIXME: this only supports grub (no lilo support)
    def trigger_addbootablekernel(self):
        boot_mount = False
        if os.path.ismount("/boot"):
            boot_mount = True
        kernels = [x for x in self.pkgdata['content'] if x.startswith("/boot/kernel-")]
        if boot_mount:
            kernels = [x[len("/boot"):] for x in kernels]
        for kernel in kernels:
            mykernel = kernel.split('/kernel-')[1]
            initramfs = "/boot/initramfs-"+mykernel
            if initramfs not in self.pkgdata['content']:
                initramfs = ''
            elif boot_mount:
                initramfs = initramfs[len("/boot"):]

            # configure GRUB
            self.Entropy.clientLog.log(
                ETP_LOGPRI_INFO,
                ETP_LOGLEVEL_NORMAL,
                "[POST] Configuring GRUB bootloader. Adding the new kernel..."
            )
            mytxt = "%s. %s ..." % (
                _("Configuring GRUB bootloader"),
                _("Adding the new kernel"),
            )
            self.Entropy.updateProgress(
                brown(mytxt),
                importance = 0,
                header = red("   ## ")
            )
            self.trigger_configure_boot_grub(kernel,initramfs)

    # FIXME: this only supports grub (no lilo support)
    def trigger_removebootablekernel(self):
        kernels = [x for x in self.pkgdata['content'] if x.startswith("/boot/kernel-")]
        for kernel in kernels:
            initramfs = "/boot/initramfs-"+kernel[13:]
            if initramfs not in self.pkgdata['content']:
                initramfs = ''
            # configure GRUB
            self.Entropy.clientLog.log(
                ETP_LOGPRI_INFO,
                ETP_LOGLEVEL_NORMAL,
                "[POST] Configuring GRUB bootloader. Removing the selected kernel..."
            )
            mytxt = "%s. %s ..." % (
                _("Configuring GRUB bootloader"),
                _("Removing the selected kernel"),
            )
            self.Entropy.updateProgress(
                mytxt,
                importance = 0,
                header = red("   ## ")
            )
            self.trigger_remove_boot_grub(kernel,initramfs)

    def trigger_mountboot(self):
        # is in fstab?
        if etpConst['systemroot']:
            return
        if os.path.isfile("/etc/fstab"):
            f = open("/etc/fstab","r")
            fstab = f.readlines()
            fstab = self.Entropy.entropyTools.listToUtf8(fstab)
            f.close()
            for line in fstab:
                fsline = line.split()
                if len(fsline) > 1:
                    if fsline[1] == "/boot":
                        if not os.path.ismount("/boot"):
                            # trigger mount /boot
                            rc = os.system("mount /boot &> /dev/null")
                            if rc == 0:
                                self.Entropy.clientLog.log(
                                    ETP_LOGPRI_INFO,
                                    ETP_LOGLEVEL_NORMAL,
                                    "[PRE] Mounted /boot successfully"
                                )
                                self.Entropy.updateProgress(
                                    brown(_("Mounted /boot successfully")),
                                    importance = 0,
                                    header = red("   ## ")
                                )
                            elif rc != 8192: # already mounted
                                self.Entropy.clientLog.log(
                                    ETP_LOGPRI_INFO,
                                    ETP_LOGLEVEL_NORMAL,
                                    "[PRE] Cannot mount /boot automatically !!"
                                )
                                self.Entropy.updateProgress(
                                    brown(_("Cannot mount /boot automatically !!")),
                                    importance = 0,
                                    header = red("   ## ")
                                )
                            break

    def trigger_kbuildsycoca(self):
        if etpConst['systemroot']:
            return
        kdedirs = ''
        try:
            kdedirs = os.environ['KDEDIRS']
        except:
            pass
        if kdedirs:
            dirs = kdedirs.split(":")
            for builddir in dirs:
                if os.access(builddir+"/bin/kbuildsycoca",os.X_OK):
                    if not os.path.isdir("/usr/share/services"):
                        os.makedirs("/usr/share/services")
                    os.chown("/usr/share/services",0,0)
                    os.chmod("/usr/share/services",0755)
                    self.Entropy.clientLog.log(
                        ETP_LOGPRI_INFO,
                        ETP_LOGLEVEL_NORMAL,
                        "[POST] Running kbuildsycoca to build global KDE database"
                    )
                    self.Entropy.updateProgress(
                        brown(_("Running kbuildsycoca to build global KDE database")),
                        importance = 0,
                        header = red("   ## ")
                    )
                    os.system(builddir+"/bin/kbuildsycoca --global --noincremental &> /dev/null")

    def trigger_kbuildsycoca4(self):
        if etpConst['systemroot']:
            return
        kdedirs = ''
        try:
            kdedirs = os.environ['KDEDIRS']
        except:
            pass
        if kdedirs:
            dirs = kdedirs.split(":")
            for builddir in dirs:
                if os.access(builddir+"/bin/kbuildsycoca4",os.X_OK):
                    if not os.path.isdir("/usr/share/services"):
                        os.makedirs("/usr/share/services")
                    os.chown("/usr/share/services",0,0)
                    os.chmod("/usr/share/services",0755)
                    self.Entropy.clientLog.log(
                        ETP_LOGPRI_INFO,
                        ETP_LOGLEVEL_NORMAL,
                        "[POST] Running kbuildsycoca4 to build global KDE4 database"
                    )
                    self.Entropy.updateProgress(
                        brown(_("Running kbuildsycoca to build global KDE database")),
                        importance = 0,
                        header = red("   ## ")
                    )
                    # do it
                    kbuild4cmd = """

                    # Thanks to the hard work of kde4 gentoo overlay maintainers

                    for i in $(dbus-launch); do
                            export "$i"
                    done

                    # This is needed because we support multiple kde versions installed together.
                    XDG_DATA_DIRS="/usr/share:${KDEDIRS}/share:/usr/local/share"
                    """+builddir+"""/bin/kbuildsycoca4 --global --noincremental &> /dev/null
                    kill ${DBUS_SESSION_BUS_PID}

                    """
                    os.system(kbuild4cmd)

    def trigger_gconfinstallschemas(self):
        gtest = os.system("which gconftool-2 &> /dev/null")
        if gtest == 0 or etpConst['systemroot']:
            schemas = [x for x in self.pkgdata['content'] if x.startswith("/etc/gconf/schemas") and x.endswith(".schemas")]
            mytxt = "%s ..." % (brown(_("Installing gconf2 schemas")),)
            self.Entropy.updateProgress(
                brown(mytxt),
                importance = 0,
                header = red("   ## ")
            )
            for schema in schemas:
                if not etpConst['systemroot']:
                    os.system("""
                    unset GCONF_DISABLE_MAKEFILE_SCHEMA_INSTALL
                    export GCONF_CONFIG_SOURCE=$(gconftool-2 --get-default-source)
                    gconftool-2 --makefile-install-rule """+schema+""" 1>/dev/null
                    """)
                else:
                    os.system(""" echo "
                    unset GCONF_DISABLE_MAKEFILE_SCHEMA_INSTALL
                    export GCONF_CONFIG_SOURCE=$(gconftool-2 --get-default-source)
                    gconftool-2 --makefile-install-rule """+schema+""" " | chroot """+etpConst['systemroot']+""" &>/dev/null
                    """)

    def trigger_pygtksetup(self):
        python_sym_files = [x for x in self.pkgdata['content'] if x.endswith("pygtk.py-2.0") or x.endswith("pygtk.pth-2.0")]
        for item in python_sym_files:
            item = etpConst['systemroot']+item
            filepath = item[:-4]
            sympath = os.path.basename(item)
            if os.path.isfile(item):
                try:
                    if os.path.lexists(filepath):
                        os.remove(filepath)
                    os.symlink(sympath,filepath)
                except OSError:
                    pass

    def trigger_pygtkremove(self):
        python_sym_files = [x for x in self.pkgdata['content'] if x.startswith("/usr/lib/python") and (x.endswith("pygtk.py-2.0") or x.endswith("pygtk.pth-2.0"))]
        for item in python_sym_files:
            item = etpConst['systemroot']+item
            if os.path.isfile(item[:-4]):
                os.remove(item[:-4])

    def trigger_susetuid(self):
        if os.path.isfile(etpConst['systemroot']+"/bin/su"):
            self.Entropy.updateProgress(
                                    brown(" Configuring '"+etpConst['systemroot']+"/bin/su' executable SETUID"),
                                    importance = 0,
                                    header = red("   ##")
                                )
            os.chown(etpConst['systemroot']+"/bin/su",0,0)
            os.system("chmod 4755 "+etpConst['systemroot']+"/bin/su")
            #os.chmod("/bin/su",4755) #FIXME: probably there's something I don't know here since, masks?

    def trigger_cleanpy(self):
        pyfiles = [x for x in self.pkgdata['content'] if x.endswith(".py")]
        for item in pyfiles:
            item = etpConst['systemroot']+item
            if os.path.isfile(item+"o"):
                try: os.remove(item+"o")
                except OSError: pass
            if os.path.isfile(item+"c"):
                try: os.remove(item+"c")
                except OSError: pass

    def trigger_createkernelsym(self):
        for item in self.pkgdata['content']:
            item = etpConst['systemroot']+item
            if item.startswith(etpConst['systemroot']+"/usr/src/"):
                # extract directory
                try:
                    todir = item[len(etpConst['systemroot']):]
                    todir = todir.split("/")[3]
                except:
                    continue
                if os.path.isdir(etpConst['systemroot']+"/usr/src/"+todir):
                    # link to /usr/src/linux
                    self.Entropy.clientLog.log(
                        ETP_LOGPRI_INFO,
                        ETP_LOGLEVEL_NORMAL,
                        "[POST] Creating kernel symlink "+etpConst['systemroot']+"/usr/src/linux for /usr/src/"+todir
                    )
                    mytxt = "%s %s %s %s" % (
                        _("Creating kernel symlink"),
                        etpConst['systemroot']+"/usr/src/linux",
                        _("for"),
                        "/usr/src/"+todir,
                    )
                    self.Entropy.updateProgress(
                        brown(mytxt),
                        importance = 0,
                        header = red("   ## ")
                    )
                    if os.path.isfile(etpConst['systemroot']+"/usr/src/linux") or \
                        os.path.islink(etpConst['systemroot']+"/usr/src/linux"):
                            os.remove(etpConst['systemroot']+"/usr/src/linux")
                    if os.path.isdir(etpConst['systemroot']+"/usr/src/linux"):
                        mydir = etpConst['systemroot']+"/usr/src/linux."+str(self.Entropy.entropyTools.getRandomNumber())
                        while os.path.isdir(mydir):
                            mydir = etpConst['systemroot']+"/usr/src/linux."+str(self.Entropy.entropyTools.getRandomNumber())
                        shutil.move(etpConst['systemroot']+"/usr/src/linux",mydir)
                    try:
                        os.symlink(todir,etpConst['systemroot']+"/usr/src/linux")
                    except OSError: # not important in the end
                        pass
                    break

    def trigger_run_ldconfig(self):
        if not etpConst['systemroot']:
            myroot = "/"
        else:
            myroot = etpConst['systemroot']+"/"
        self.Entropy.clientLog.log(
            ETP_LOGPRI_INFO,
            ETP_LOGLEVEL_NORMAL,
            "[POST] Running ldconfig"
        )
        mytxt = "%s %s" % (_("Regenerating"),"/etc/ld.so.cache",)
        self.Entropy.updateProgress(
            brown(mytxt),
            importance = 0,
            header = red("   ## ")
        )
        os.system("ldconfig -r "+myroot+" &> /dev/null")

    def trigger_env_update(self):
        # clear linker paths cache
        del linkerPaths[:]
        self.Entropy.clientLog.log(
            ETP_LOGPRI_INFO,
            ETP_LOGLEVEL_NORMAL,
            "[POST] Running env-update"
        )
        if os.access(etpConst['systemroot']+"/usr/sbin/env-update",os.X_OK):
            mytxt = "%s ..." % (_("Updating environment"),)
            self.Entropy.updateProgress(
                brown(mytxt),
                importance = 0,
                header = red("   ## ")
            )
            if etpConst['systemroot']:
                os.system("echo 'env-update --no-ldconfig' | chroot "+etpConst['systemroot']+" &> /dev/null")
            else:
                os.system('env-update --no-ldconfig &> /dev/null')

    def trigger_add_java_config_2(self):
        vms = set()
        for vm in self.pkgdata['content']:
            vm = etpConst['systemroot']+vm
            if vm.startswith(etpConst['systemroot']+"/usr/share/java-config-2/vm/") and os.path.isfile(vm):
                vms.add(vm)
        # sort and get the latter
        if vms:
            vms = list(vms)
            vms.reverse()
            myvm = vms[0].split("/")[-1]
            if myvm:
                if os.access(etpConst['systemroot']+"/usr/bin/java-config",os.X_OK):
                    self.Entropy.clientLog.log(
                        ETP_LOGPRI_INFO,
                        ETP_LOGLEVEL_NORMAL,
                        "[POST] Configuring JAVA using java-config with VM: "+myvm
                    )
                    # set
                    mytxt = "%s %s %s" % (
                        brown(_("Setting system VM to")),
                        bold(str(myvm)),
                        brown("..."),
                    )
                    self.Entropy.updateProgress(
                        mytxt,
                        importance = 0,
                        header = red("   ## ")
                    )
                    if not etpConst['systemroot']:
                        os.system("java-config -S "+myvm)
                    else:
                        os.system("echo 'java-config -S "+myvm+"' | chroot "+etpConst['systemroot']+" &> /dev/null")
                else:
                    self.Entropy.clientLog.log(
                        ETP_LOGPRI_INFO,
                        ETP_LOGLEVEL_NORMAL,
                        "[POST] ATTENTION /usr/bin/java-config does not exist. I was about to set JAVA VM: "+myvm
                    )
                    mytxt = "%s: %s %s. %s." % (
                        bold(_("Attention")),
                        brown("/usr/bin/java-config"),
                        brown(_("does not exist")),
                        brown("Cannot set JAVA VM"),
                    )
                    self.Entropy.updateProgress(
                        mytxt,
                        importance = 0,
                        header = red("   ## ")
                    )
        del vms

    def trigger_ebuild_postinstall(self):
        stdfile = open("/dev/null","w")
        oldstderr = sys.stderr
        oldstdout = sys.stdout
        sys.stderr = stdfile

        myebuild = [self.pkgdata['xpakdir']+"/"+x for x in os.listdir(self.pkgdata['xpakdir']) if x.endswith(".ebuild")]
        if myebuild:
            myebuild = myebuild[0]
            portage_atom = self.pkgdata['category']+"/"+self.pkgdata['name']+"-"+self.pkgdata['version']
            self.Entropy.updateProgress(
                brown("Ebuild: pkg_postinst()"),
                importance = 0,
                header = red("   ##")
            )
            try:

                if not os.path.isfile(self.pkgdata['unpackdir']+"/portage/"+portage_atom+"/temp/environment"):
                    # if environment is not yet created, we need to run pkg_setup()
                    sys.stdout = stdfile
                    rc = self.Spm.spm_doebuild(
                        myebuild,
                        mydo = "setup",
                        tree = "bintree",
                        cpv = portage_atom,
                        portage_tmpdir = self.pkgdata['unpackdir'],
                        licenses = self.pkgdata['accept_license']
                    )
                    if rc == 1:
                        self.Entropy.clientLog.log(
                            ETP_LOGPRI_INFO,
                            ETP_LOGLEVEL_NORMAL,
                            "[POST] ATTENTION Cannot properly run Gentoo postinstall (pkg_setup())"
                            " trigger for "+str(portage_atom)+". Something bad happened."
                        )
                    sys.stdout = oldstdout

                rc = self.Spm.spm_doebuild(
                    myebuild,
                    mydo = "postinst",
                    tree = "bintree",
                    cpv = portage_atom,
                    portage_tmpdir = self.pkgdata['unpackdir'],
                    licenses = self.pkgdata['accept_license']
                )
                if rc == 1:
                    self.Entropy.clientLog.log(
                        ETP_LOGPRI_INFO,
                        ETP_LOGLEVEL_NORMAL,
                        "[POST] ATTENTION Cannot properly run Gentoo postinstall (pkg_postinst()) trigger for " + \
                        str(portage_atom) + ". Something bad happened."
                        )

            except Exception, e:
                sys.stdout = oldstdout
                self.entropyTools.printTraceback()
                self.Entropy.clientLog.log(
                    ETP_LOGPRI_INFO,
                    ETP_LOGLEVEL_NORMAL,
                    "[POST] ATTENTION Cannot run Portage trigger for "+portage_atom+"!! "+str(Exception)+": "+str(e)
                )
                mytxt = "%s: %s %s. %s." % (
                    bold(_("QA")),
                    brown(_("Cannot run Portage trigger for")),
                    bold(str(portage_atom)),
                    brown(_("Please report it")),
                )
                self.Entropy.updateProgress(
                    mytxt,
                    importance = 0,
                    header = red("   ## ")
                )
        sys.stderr = oldstderr
        sys.stdout = oldstdout
        stdfile.close()
        return 0

    def trigger_ebuild_preinstall(self):
        stdfile = open("/dev/null","w")
        oldstderr = sys.stderr
        oldstdout = sys.stdout
        sys.stderr = stdfile

        myebuild = [self.pkgdata['xpakdir']+"/"+x for x in os.listdir(self.pkgdata['xpakdir']) if x.endswith(".ebuild")]
        if myebuild:
            myebuild = myebuild[0]
            portage_atom = self.pkgdata['category']+"/"+self.pkgdata['name']+"-"+self.pkgdata['version']
            self.Entropy.updateProgress(
                brown(" Ebuild: pkg_preinst()"),
                importance = 0,
                header = red("   ##")
            )
            try:
                sys.stdout = stdfile
                rc = self.Spm.spm_doebuild(
                    myebuild,
                    mydo = "setup",
                    tree = "bintree",
                    cpv = portage_atom,
                    portage_tmpdir = self.pkgdata['unpackdir'],
                    licenses = self.pkgdata['accept_license']
                ) # create mysettings["T"]+"/environment"
                if rc == 1:
                    self.Entropy.clientLog.log(
                        ETP_LOGPRI_INFO,
                        ETP_LOGLEVEL_NORMAL,
                        "[PRE] ATTENTION Cannot properly run Portage preinstall (pkg_setup()) trigger for " + \
                        str(portage_atom) + ". Something bad happened."
                    )
                sys.stdout = oldstdout
                rc = self.Spm.spm_doebuild(
                    myebuild,
                    mydo = "preinst",
                    tree = "bintree",
                    cpv = portage_atom,
                    portage_tmpdir = self.pkgdata['unpackdir'],
                    licenses = self.pkgdata['accept_license']
                )
                if rc == 1:
                    self.Entropy.clientLog.log(
                        ETP_LOGPRI_INFO,
                        ETP_LOGLEVEL_NORMAL,
                        "[PRE] ATTENTION Cannot properly run Gentoo preinstall (pkg_preinst()) trigger for " + \
                        str(portage_atom)+". Something bad happened."
                    )
            except Exception, e:
                sys.stdout = oldstdout
                self.entropyTools.printTraceback()
                self.Entropy.clientLog.log(
                    ETP_LOGPRI_INFO,
                    ETP_LOGLEVEL_NORMAL,
                    "[PRE] ATTENTION Cannot run Gentoo preinst trigger for "+portage_atom+"!! "+str(Exception)+": "+str(e)
                )
                mytxt = "%s: %s %s. %s." % (
                    bold(_("QA")),
                    brown(_("Cannot run Portage trigger for")),
                    bold(str(portage_atom)),
                    brown(_("Please report it")),
                )
                self.Entropy.updateProgress(
                    mytxt,
                    importance = 0,
                    header = red("   ## ")
                )
        sys.stderr = oldstderr
        sys.stdout = oldstdout
        stdfile.close()
        return 0

    def trigger_ebuild_preremove(self):
        stdfile = open("/dev/null","w")
        oldstderr = sys.stderr
        sys.stderr = stdfile

        portage_atom = self.pkgdata['category']+"/"+self.pkgdata['name']+"-"+self.pkgdata['version']
        try:
            myebuild = self.Spm.get_vdb_path()+portage_atom+"/"+self.pkgdata['name']+"-"+self.pkgdata['version']+".ebuild"
        except:
            myebuild = ''

        self.myebuild_moved = None
        if os.path.isfile(myebuild):
            try:
                myebuild = self._setup_remove_ebuild_environment(myebuild, portage_atom)
            except EOFError, e:
                sys.stderr = oldstderr
                stdfile.close()
                # stuff on system is broken, ignore it
                self.Entropy.updateProgress(
                    darkred("!!! Ebuild: pkg_prerm() failed, EOFError: ")+str(e)+darkred(" - ignoring"),
                    importance = 1,
                    type = "warning",
                    header = red("   ## ")
                )
                return 0

        if os.path.isfile(myebuild):

            self.Entropy.updateProgress(
                                    brown(" Ebuild: pkg_prerm()"),
                                    importance = 0,
                                    header = red("   ##")
                                )
            try:
                rc = self.Spm.spm_doebuild(
                    myebuild,
                    mydo = "prerm",
                    tree = "bintree",
                    cpv = portage_atom,
                    portage_tmpdir = etpConst['entropyunpackdir'] + "/" + portage_atom
                )
                if rc == 1:
                    self.Entropy.clientLog.log(
                        ETP_LOGPRI_INFO,
                        ETP_LOGLEVEL_NORMAL,
                        "[PRE] ATTENTION Cannot properly run Portage trigger for " + \
                        str(portage_atom)+". Something bad happened."
                    )
            except Exception, e:
                sys.stderr = oldstderr
                stdfile.close()
                self.entropyTools.printTraceback()
                self.Entropy.clientLog.log(
                    ETP_LOGPRI_INFO,
                    ETP_LOGLEVEL_NORMAL,
                    "[PRE] ATTENTION Cannot run Portage preremove trigger for "+portage_atom+"!! "+str(Exception)+": "+str(e)
                )
                mytxt = "%s: %s %s. %s." % (
                    bold(_("QA")),
                    brown(_("Cannot run Portage trigger for")),
                    bold(str(portage_atom)),
                    brown(_("Please report it")),
                )
                self.Entropy.updateProgress(
                    mytxt,
                    importance = 0,
                    header = red("   ## ")
                )
                return 0

        sys.stderr = oldstderr
        stdfile.close()
        self._remove_overlayed_ebuild()
        return 0

    def trigger_ebuild_postremove(self):
        stdfile = open("/dev/null","w")
        oldstderr = sys.stderr
        sys.stderr = stdfile

        portage_atom = self.pkgdata['category']+"/"+self.pkgdata['name']+"-"+self.pkgdata['version']
        try:
            myebuild = self.Spm.get_vdb_path()+portage_atom+"/"+self.pkgdata['name']+"-"+self.pkgdata['version']+".ebuild"
        except:
            myebuild = ''

        self.myebuild_moved = None
        if os.path.isfile(myebuild):
            myebuild = self._setup_remove_ebuild_environment(myebuild, portage_atom)

        if os.path.isfile(myebuild):
            self.Entropy.updateProgress(
                                    brown(" Ebuild: pkg_postrm()"),
                                    importance = 0,
                                    header = red("   ##")
                                )
            try:
                rc = self.Spm.spm_doebuild(
                    myebuild,
                    mydo = "postrm",
                    tree = "bintree",
                    cpv = portage_atom,
                    portage_tmpdir = etpConst['entropyunpackdir']+"/"+portage_atom
                )
                if rc == 1:
                    self.Entropy.clientLog.log(
                        ETP_LOGPRI_INFO,
                        ETP_LOGLEVEL_NORMAL,
                        "[PRE] ATTENTION Cannot properly run Gentoo postremove trigger for " + \
                        str(portage_atom)+". Something bad happened."
                    )
            except Exception, e:
                sys.stderr = oldstderr
                stdfile.close()
                self.entropyTools.printTraceback()
                self.Entropy.clientLog.log(
                    ETP_LOGPRI_INFO,
                    ETP_LOGLEVEL_NORMAL,
                    "[PRE] ATTENTION Cannot run Gentoo postremove trigger for " + \
                    portage_atom+"!! "+str(Exception)+": "+str(e)
                )
                mytxt = "%s: %s %s. %s." % (
                    bold(_("QA")),
                    brown(_("Cannot run Portage trigger for")),
                    bold(str(portage_atom)),
                    brown(_("Please report it")),
                )
                self.Entropy.updateProgress(
                    mytxt,
                    importance = 0,
                    header = red("   ## ")
                )
                return 0

        sys.stderr = oldstderr
        stdfile.close()
        self._remove_overlayed_ebuild()
        return 0

    def _setup_remove_ebuild_environment(self, myebuild, portage_atom):

        ebuild_dir = os.path.dirname(myebuild)
        ebuild_file = os.path.basename(myebuild)

        # copy the whole directory in a safe place
        dest_dir = os.path.join(etpConst['entropyunpackdir'],"vardb/"+portage_atom)
        if os.path.exists(dest_dir):
            if os.path.isdir(dest_dir):
                shutil.rmtree(dest_dir,True)
            elif os.path.isfile(dest_dir) or os.path.islink(dest_dir):
                os.remove(dest_dir)
        os.makedirs(dest_dir)
        items = os.listdir(ebuild_dir)
        for item in items:
            myfrom = os.path.join(ebuild_dir,item)
            myto = os.path.join(dest_dir,item)
            shutil.copy2(myfrom,myto)

        newmyebuild = os.path.join(dest_dir,ebuild_file)
        if os.path.isfile(newmyebuild):
            myebuild = newmyebuild
            self.myebuild_moved = myebuild
            self._ebuild_env_setup_hook(myebuild)
        return myebuild

    def _ebuild_env_setup_hook(self, myebuild):
        ebuild_path = os.path.dirname(myebuild)
        if not etpConst['systemroot']:
            myroot = "/"
        else:
            myroot = etpConst['systemroot']+"/"

        # we need to fix ROOT= if it's set inside environment
        bz2envfile = os.path.join(ebuild_path,"environment.bz2")
        if os.path.isfile(bz2envfile) and os.path.isdir(myroot):
            import bz2
            envfile = self.Entropy.entropyTools.unpackBzip2(bz2envfile)
            bzf = bz2.BZ2File(bz2envfile,"w")
            f = open(envfile,"r")
            line = f.readline()
            while line:
                if line.startswith("ROOT="):
                    line = "ROOT=%s\n" % (myroot,)
                bzf.write(line)
                line = f.readline()
            f.close()
            bzf.close()
            os.remove(envfile)

    def _remove_overlayed_ebuild(self):
        if not self.myebuild_moved:
            return

        if os.path.isfile(self.myebuild_moved):
            mydir = os.path.dirname(self.myebuild_moved)
            shutil.rmtree(mydir,True)
            mydir = os.path.dirname(mydir)
            content = os.listdir(mydir)
            while not content:
                os.rmdir(mydir)
                mydir = os.path.dirname(mydir)
                content = os.listdir(mydir)

    '''
        Internal ones
    '''

    '''
    @description: creates Xfont files
    @output: returns int() as exit status
    '''
    def trigger_setup_font_dir(self, fontdir):
        # mkfontscale
        if os.access('/usr/bin/mkfontscale',os.X_OK):
            os.system('/usr/bin/mkfontscale '+unicode(fontdir))
        # mkfontdir
        if os.access('/usr/bin/mkfontdir',os.X_OK):
            os.system('/usr/bin/mkfontdir -e '+etpConst['systemroot']+'/usr/share/fonts/encodings -e '+etpConst['systemroot']+'/usr/share/fonts/encodings/large '+unicode(fontdir))
        return 0

    '''
    @description: creates font cache
    @output: returns int() as exit status
    '''
    def trigger_setup_font_cache(self, fontdir):
        # fc-cache -f gooooo!
        if os.access('/usr/bin/fc-cache',os.X_OK):
            os.system('/usr/bin/fc-cache -f '+unicode(fontdir))
        return 0

    '''
    @description: set chosen gcc profile
    @output: returns int() as exit status
    '''
    def trigger_set_gcc_profile(self, profile):
        if os.access(etpConst['systemroot']+'/usr/bin/gcc-config',os.X_OK):
            redirect = ""
            if etpUi['quiet']:
                redirect = " &> /dev/null"
            if etpConst['systemroot']:
                os.system("echo '/usr/bin/gcc-config "+profile+"' | chroot "+etpConst['systemroot']+redirect)
            else:
                os.system('/usr/bin/gcc-config '+profile+redirect)
        return 0

    '''
    @description: set chosen binutils profile
    @output: returns int() as exit status
    '''
    def trigger_set_binutils_profile(self, profile):
        if os.access(etpConst['systemroot']+'/usr/bin/binutils-config',os.X_OK):
            redirect = ""
            if etpUi['quiet']:
                redirect = " &> /dev/null"
            if etpConst['systemroot']:
                os.system("echo '/usr/bin/binutils-config "+profile+"' | chroot "+etpConst['systemroot']+redirect)
            else:
                os.system('/usr/bin/binutils-config '+profile+redirect)
        return 0

    '''
    @description: creates/updates icons cache
    @output: returns int() as exit status
    '''
    def trigger_generate_icons_cache(self, cachedir):
        if not etpConst['systemroot']:
            myroot = "/"
        else:
            myroot = etpConst['systemroot']+"/"
        if os.access('/usr/bin/gtk-update-icon-cache',os.X_OK):
            os.system('ROOT="'+myroot+'" /usr/bin/gtk-update-icon-cache -qf '+cachedir)
        return 0

    '''
    @description: updates /usr/share/mime database
    @output: returns int() as exit status
    '''
    def trigger_update_mime_db(self):
        if os.access(etpConst['systemroot']+'/usr/bin/update-mime-database',os.X_OK):
            if not etpConst['systemroot']:
                os.system('/usr/bin/update-mime-database /usr/share/mime')
            else:
                os.system("echo '/usr/bin/update-mime-database /usr/share/mime' | chroot "+etpConst['systemroot']+" &> /dev/null")
        return 0

    '''
    @description: updates /usr/share/applications database
    @output: returns int() as exit status
    '''
    def trigger_update_mime_desktop_db(self):
        if os.access(etpConst['systemroot']+'/usr/bin/update-desktop-database',os.X_OK):
            if not etpConst['systemroot']:
                os.system('/usr/bin/update-desktop-database -q /usr/share/applications')
            else:
                os.system("echo '/usr/bin/update-desktop-database -q /usr/share/applications' | chroot "+etpConst['systemroot']+" &> /dev/null")
        return 0

    '''
    @description: updates /var/lib/scrollkeeper database
    @output: returns int() as exit status
    '''
    def trigger_update_scrollkeeper_db(self):
        if os.access(etpConst['systemroot']+'/usr/bin/scrollkeeper-update',os.X_OK):
            if not os.path.isdir(etpConst['systemroot']+'/var/lib/scrollkeeper'):
                os.makedirs(etpConst['systemroot']+'/var/lib/scrollkeeper')
            if not etpConst['systemroot']:
                os.system('/usr/bin/scrollkeeper-update -q -p /var/lib/scrollkeeper')
            else:
                os.system("echo '/usr/bin/scrollkeeper-update -q -p /var/lib/scrollkeeper' | chroot "+etpConst['systemroot']+" &> /dev/null")
        return 0

    '''
    @description: respawn gconfd-2 if found
    @output: returns int() as exit status
    '''
    def trigger_reload_gconf_db(self):
        if etpConst['systemroot']:
            return 0
        rc = os.system('pgrep -x gconfd-2')
        if (rc == 0):
            pids = commands.getoutput('pgrep -x gconfd-2').split("\n")
            pidsstr = ''
            for pid in pids:
                if pid:
                    pidsstr += pid+' '
            pidsstr = pidsstr.strip()
            if pidsstr:
                os.system('kill -HUP '+pidsstr)
        return 0

    '''
    @description: updates moduledb
    @output: returns int() as exit status
    '''
    def trigger_update_moduledb(self, item):
        if os.access(etpConst['systemroot']+'/usr/sbin/module-rebuild',os.X_OK):
            if os.path.isfile(etpConst['systemroot']+self.MODULEDB_DIR+'moduledb'):
                f = open(etpConst['systemroot']+self.MODULEDB_DIR+'moduledb',"r")
                moduledb = f.readlines()
                moduledb = self.Entropy.entropyTools.listToUtf8(moduledb)
                f.close()
                avail = [x for x in moduledb if x.strip() == item]
                if (not avail):
                    f = open(etpConst['systemroot']+self.MODULEDB_DIR+'moduledb',"aw")
                    f.write(item+"\n")
                    f.flush()
                    f.close()
        return 0

    '''
    @description: insert kernel object into kernel modules db
    @output: returns int() as exit status
    '''
    def trigger_run_depmod(self, name):
        if os.access('/sbin/depmod',os.X_OK):
            if not etpConst['systemroot']:
                myroot = "/"
            else:
                myroot = etpConst['systemroot']+"/"
            os.system('/sbin/depmod -a -b '+myroot+' -r '+name+' &> /dev/null')
        return 0

    '''
    @description: update /usr/bin/python and /usr/bin/python2 symlink
    @output: returns int() as exit status
    '''
    def trigger_python_update_symlink(self):
        bins = [x for x in os.listdir("/usr/bin") if x.startswith("python2.")]
        if bins: # don't ask me why but it happened...
            bins.sort()
            latest = bins[-1]

            latest = etpConst['systemroot']+"/usr/bin/"+latest
            filepath = os.path.dirname(latest)+"/python"
            sympath = os.path.basename(latest)
            if os.path.isfile(latest):
                try:
                    if os.path.lexists(filepath):
                        os.remove(filepath)
                    os.symlink(sympath,filepath)
                except OSError:
                    pass
        return 0

    '''
    @description: update /usr/bin/lemon symlink
    @output: returns int() as exit status
    '''
    def trigger_sqlite_update_symlink(self):
        bins = [x for x in os.listdir("/usr/bin") if x.startswith("lemon-")]
        if bins:
            bins.sort()
            latest = bins[-1]
            latest = etpConst['systemroot']+"/usr/bin/"+latest

            filepath = os.path.dirname(latest)+"/lemon"
            sympath = os.path.basename(latest)
            if os.path.isfile(latest):
                try:
                    if os.path.lexists(filepath):
                        os.remove(filepath)
                    os.symlink(sympath,filepath)
                except OSError:
                    pass
        return 0

    '''
    @description: shuts down selected init script, and remove from runlevel
    @output: returns int() as exit status
    '''
    def trigger_initdeactivate(self, item, scheduled):
        if not etpConst['systemroot']:
            myroot = "/"
            '''
            causes WORLD to fall under
            if (running):
                os.system(item+' stop --quiet')
            '''
        else:
            myroot = etpConst['systemroot']+"/"
        if (scheduled):
            os.system('ROOT="'+myroot+'" rc-update del '+os.path.basename(item))
        return 0

    def __get_entropy_kernel_grub_line(self, kernel):
        return "title="+etpConst['systemname']+" ("+os.path.basename(kernel)+")\n"

    '''
    @description: append kernel entry to grub.conf
    @output: returns int() as exit status
    '''
    def trigger_configure_boot_grub(self, kernel,initramfs):

        if not os.path.isdir(etpConst['systemroot']+"/boot/grub"):
            os.makedirs(etpConst['systemroot']+"/boot/grub")
        if os.path.isfile(etpConst['systemroot']+"/boot/grub/grub.conf"):
            # open in append
            grub = open(etpConst['systemroot']+"/boot/grub/grub.conf","aw")
            shutil.copy2(etpConst['systemroot']+"/boot/grub/grub.conf",etpConst['systemroot']+"/boot/grub/grub.conf.old.add")
            # get boot dev
            boot_dev = self.trigger_get_grub_boot_dev()
            # test if entry has been already added
            grubtest = open(etpConst['systemroot']+"/boot/grub/grub.conf","r")
            content = grubtest.readlines()
            content = [unicode(x,'raw_unicode_escape') for x in content]
            for line in content:
                if line.find(self.__get_entropy_kernel_grub_line(kernel)) != -1:
                    grubtest.close()
                    return
                # also check if we have the same kernel listed
                if (line.find("kernel") != 1) and (line.find(os.path.basename(kernel)) != -1) and not line.strip().startswith("#"):
                    grubtest.close()
                    return
        else:
            # create
            boot_dev = "(hd0,0)"
            grub = open(etpConst['systemroot']+"/boot/grub/grub.conf","w")
            # write header - guess (hd0,0)... since it is weird having a running system without a bootloader, at least, grub.
            grub_header = '''
default=0
timeout=10
            '''
            grub.write(grub_header)
        cmdline = ' '
        if os.path.isfile("/proc/cmdline"):
            f = open("/proc/cmdline","r")
            cmdline = " "+f.readline().strip()
            params = cmdline.split()
            if "dolvm" not in params: # support new kernels >= 2.6.23
                cmdline += " dolvm "
            f.close()
        grub.write(self.__get_entropy_kernel_grub_line(kernel))
        grub.write("\troot "+boot_dev+"\n")
        grub.write("\tkernel "+kernel+cmdline+"\n")
        if initramfs:
            grub.write("\tinitrd "+initramfs+"\n")
        grub.write("\n")
        grub.flush()
        grub.close()

    def trigger_remove_boot_grub(self, kernel,initramfs):
        if os.path.isdir(etpConst['systemroot']+"/boot/grub") and os.path.isfile(etpConst['systemroot']+"/boot/grub/grub.conf"):
            shutil.copy2(etpConst['systemroot']+"/boot/grub/grub.conf",etpConst['systemroot']+"/boot/grub/grub.conf.old.remove")
            f = open(etpConst['systemroot']+"/boot/grub/grub.conf","r")
            grub_conf = f.readlines()
            f.close()
            content = [unicode(x,'raw_unicode_escape') for x in grub_conf]
            try:
                kernel, initramfs = (unicode(kernel,'raw_unicode_escape'),unicode(initramfs,'raw_unicode_escape'))
            except TypeError:
                pass
            #kernelname = os.path.basename(kernel)
            new_conf = []
            skip = False
            for line in content:

                if (line.find(self.__get_entropy_kernel_grub_line(kernel)) != -1):
                    skip = True
                    continue

                if line.strip().startswith("title"):
                    skip = False

                if not skip or line.strip().startswith("#"):
                    new_conf.append(line)

            f = open(etpConst['systemroot']+"/boot/grub/grub.conf","w")
            for line in new_conf:
                f.write(line)
            f.flush()
            f.close()

    def trigger_get_grub_boot_dev(self):
        if etpConst['systemroot']:
            return "(hd0,0)"
        import re
        df_avail = os.system("which df &> /dev/null")
        if df_avail != 0:
            mytxt = "%s: %s! %s. %s (hd0,0)." % (
                bold(_("QA")),
                brown(_("Cannot find df")),
                brown(_("Cannot properly configure the kernel")),
                brown(_("Defaulting to")),
            )
            self.Entropy.updateProgress(
                mytxt,
                importance = 0,
                header = red("   ## ")
            )
            return "(hd0,0)"
        grub_avail = os.system("which grub &> /dev/null")
        if grub_avail != 0:
            mytxt = "%s: %s! %s. %s (hd0,0)." % (
                bold(_("QA")),
                brown(_("Cannot find grub")),
                brown(_("Cannot properly configure the kernel")),
                brown(_("Defaulting to")),
            )
            self.Entropy.updateProgress(
                mytxt,
                importance = 0,
                header = red("   ## ")
            )
            return "(hd0,0)"

        gboot = commands.getoutput("df /boot").split("\n")[-1].split()[0]
        if gboot.startswith("/dev/"):
            # it's ok - handle /dev/md
            if gboot.startswith("/dev/md"):
                md = os.path.basename(gboot)
                if not md.startswith("md"):
                    md = "md"+md
                f = open("/proc/mdstat","r")
                mdstat = f.readlines()
                mdstat = [x for x in mdstat if x.startswith(md)]
                f.close()
                if mdstat:
                    mdstat = mdstat[0].strip().split()
                    mddevs = []
                    for x in mdstat:
                        if x.startswith("sd"):
                            mddevs.append(x[:-3])
                    mddevs.sort()
                    if mddevs:
                        gboot = "/dev/"+mddevs[0]
                    else:
                        gboot = "/dev/sda1"
                else:
                    gboot = "/dev/sda1"
            # get disk
            match = re.subn("[0-9]","",gboot)
            gdisk = match[0]
            if gdisk == '':

                mytxt = "%s: %s %s %s. %s! %s (hd0,0)." % (
                    bold(_("QA")),
                    brown(_("cannot match device")),
                    brown(str(gboot)),
                    brown(_("with a grub one")), # 'cannot match device /dev/foo with a grub one'
                    brown(_("Cannot properly configure the kernel")),
                    brown(_("Defaulting to")),
                )
                self.Entropy.updateProgress(
                    mytxt,
                    importance = 0,
                    header = red("   ## ")
                )
                return "(hd0,0)"
            match = re.subn("[a-z/]","",gboot)
            try:
                gpartnum = str(int(match[0])-1)
            except ValueError:
                mytxt = "%s: %s: %s. %s. %s (hd0,0)." % (
                    bold(_("QA")),
                    brown(_("grub translation not supported for")),
                    brown(str(gboot)),
                    brown(_("Cannot properly configure grub.conf")),
                    brown(_("Defaulting to")),
                )
                self.Entropy.updateProgress(
                    mytxt,
                    importance = 0,
                    header = red("   ## ")
                )
                return "(hd0,0)"
            # now match with grub
            device_map = etpConst['packagestmpdir']+"/grub.map"
            if os.path.isfile(device_map):
                os.remove(device_map)
            # generate device.map
            os.system('echo "quit" | grub --device-map='+device_map+' --no-floppy --batch &> /dev/null')
            if os.path.isfile(device_map):
                f = open(device_map,"r")
                device_map_file = f.readlines()
                f.close()
                grub_dev = [x for x in device_map_file if (x.find(gdisk) != -1)]
                if grub_dev:
                    grub_disk = grub_dev[0].strip().split()[0]
                    grub_dev = grub_disk[:-1]+","+gpartnum+")"
                    return grub_dev
                else:
                    mytxt = "%s: %s. %s! %s (hd0,0)." % (
                        bold(_("QA")),
                        brown(_("cannot match grub device with a Linux one")),
                        brown(_("Cannot properly configure the kernel")),
                        brown(_("Defaulting to")),
                    )
                    self.Entropy.updateProgress(
                        mytxt,
                        importance = 0,
                        header = red("   ## ")
                    )
                    return "(hd0,0)"
            else:
                mytxt = "%s: %s. %s! %s (hd0,0)." % (
                    bold(_("QA")),
                    brown(_("cannot find generated device.map")),
                    brown(_("Cannot properly configure the kernel")),
                    brown(_("Defaulting to")),
                )
                self.Entropy.updateProgress(
                    mytxt,
                    importance = 0,
                    header = red("   ## ")
                )
                return "(hd0,0)"
        else:
            mytxt = "%s: %s. %s! %s (hd0,0)." % (
                bold(_("QA")),
                brown(_("cannot run df /boot")),
                brown(_("Cannot properly configure the kernel")),
                brown(_("Defaulting to")),
            )
            self.Entropy.updateProgress(
                mytxt,
                importance = 0,
                header = red("   ## ")
            )
            return "(hd0,0)"

class PackageMaskingParser:

    def __init__(self, EquoInstance):

        if not isinstance(EquoInstance,EquoInterface):
            mytxt = _("A valid Equo instance or subclass is needed")
            raise exceptionTools.IncorrectParameter("IncorrectParameter: %s" % (mytxt,))
        self.Entropy = EquoInstance

    def parse(self):

        self.etpMaskFiles = {
            'keywords': etpConst['confdir']+"/packages/package.keywords", # keywording configuration files
            'unmask': etpConst['confdir']+"/packages/package.unmask", # unmasking configuration files
            'mask': etpConst['confdir']+"/packages/package.mask", # masking configuration files
            'license_mask': etpConst['confdir']+"/packages/license.mask", # masking configuration files
            'repos_mask': {},
            'repos_license_whitelist': {}
        }
        # append repositories mask files
        for repoid in etpRepositoriesOrder:
            maskpath = os.path.join(etpRepositories[repoid]['dbpath'],etpConst['etpdatabasemaskfile'])
            wlpath = os.path.join(etpRepositories[repoid]['dbpath'],etpConst['etpdatabaselicwhitelistfile'])
            if os.path.isfile(maskpath) and os.access(maskpath,os.R_OK):
                self.etpMaskFiles['repos_mask'][repoid] = maskpath
            if os.path.isfile(wlpath) and os.access(wlpath,os.R_OK):
                self.etpMaskFiles['repos_license_whitelist'][repoid] = wlpath

        self.etpMtimeFiles = {
            'keywords_mtime': etpConst['dumpstoragedir']+"/keywords.mtime",
            'unmask_mtime': etpConst['dumpstoragedir']+"/unmask.mtime",
            'mask_mtime': etpConst['dumpstoragedir']+"/mask.mtime",
            'license_mask_mtime': etpConst['dumpstoragedir']+"/license_mask.mtime",
            'repos_mask': {},
            'repos_license_whitelist': {}
        }
        # append repositories mtime files
        for repoid in etpRepositoriesOrder:
            if repoid in self.etpMaskFiles['repos_mask']:
                self.etpMtimeFiles['repos_mask'][repoid] = etpConst['dumpstoragedir']+"/repo_"+repoid+"_"+etpConst['etpdatabasemaskfile']+".mtime"
            if repoid in self.etpMaskFiles['repos_license_whitelist']:
                self.etpMtimeFiles['repos_license_whitelist'][repoid] = etpConst['dumpstoragedir']+"/repo_"+repoid+"_"+etpConst['etpdatabaselicwhitelistfile']+".mtime"

        data = {}
        for item in self.etpMaskFiles:
            data[item] = eval('self.'+item+'_parser')()
        return data


    '''
    parser of package.keywords file
    '''
    def keywords_parser(self):

        self.__validateEntropyCache(self.etpMaskFiles['keywords'],self.etpMtimeFiles['keywords_mtime'])

        data = {
                'universal': set(),
                'packages': {},
                'repositories': {},
        }
        if os.path.isfile(self.etpMaskFiles['keywords']):
            f = open(self.etpMaskFiles['keywords'],"r")
            content = f.readlines()
            f.close()
            # filter comments and white lines
            content = [x.strip() for x in content if not x.startswith("#") and x.strip()]
            for line in content:
                keywordinfo = line.split()
                # skip wrong lines
                if len(keywordinfo) > 3:
                    sys.stderr.write(">> "+line+" << is invalid!!")
                    continue
                if len(keywordinfo) == 1: # inversal keywording, check if it's not repo=
                    # repo=?
                    if keywordinfo[0].startswith("repo="):
                        sys.stderr.write(">> "+line+" << is invalid!!")
                        continue
                    # atom? is it worth it? it would take a little bit to parse uhm... >50 entries...!?
                    #kinfo = keywordinfo[0]
                    if keywordinfo[0] == "**": keywordinfo[0] = "" # convert into entropy format
                    data['universal'].add(keywordinfo[0])
                    continue # needed?
                if len(keywordinfo) in (2,3): # inversal keywording, check if it's not repo=
                    # repo=?
                    if keywordinfo[0].startswith("repo="):
                        sys.stderr.write(">> "+line+" << is invalid!!")
                        continue
                    # add to repo?
                    items = keywordinfo[1:]
                    if keywordinfo[0] == "**": keywordinfo[0] = "" # convert into entropy format
                    reponame = [x for x in items if x.startswith("repo=") and (len(x.split("=")) == 2)]
                    if reponame:
                        reponame = reponame[0].split("=")[1]
                        if reponame not in data['repositories']:
                            data['repositories'][reponame] = {}
                        # repository unmask or package in repository unmask?
                        if keywordinfo[0] not in data['repositories'][reponame]:
                            data['repositories'][reponame][keywordinfo[0]] = set()
                        if len(items) == 1:
                            # repository unmask
                            data['repositories'][reponame][keywordinfo[0]].add('*')
                        else:
                            if "*" not in data['repositories'][reponame][keywordinfo[0]]:
                                item = [x for x in items if not x.startswith("repo=")]
                                data['repositories'][reponame][keywordinfo[0]].add(item[0])
                    else:
                        # it's going to be a faulty line!!??
                        if len(items) == 2: # can't have two items and no repo=
                            sys.stderr.write(">> "+line+" << is invalid!!")
                            continue
                        # add keyword to packages
                        if keywordinfo[0] not in data['packages']:
                            data['packages'][keywordinfo[0]] = set()
                        data['packages'][keywordinfo[0]].add(items[0])
        return data


    def unmask_parser(self):
        self.__validateEntropyCache(self.etpMaskFiles['unmask'],self.etpMtimeFiles['unmask_mtime'])

        data = set()
        if os.path.isfile(self.etpMaskFiles['unmask']):
            f = open(self.etpMaskFiles['unmask'],"r")
            content = f.readlines()
            f.close()
            # filter comments and white lines
            content = [x.strip() for x in content if not x.startswith("#") and x.strip()]
            for line in content:
                data.add(line)
        return data

    def mask_parser(self):
        self.__validateEntropyCache(self.etpMaskFiles['mask'],self.etpMtimeFiles['mask_mtime'])

        data = set()
        if os.path.isfile(self.etpMaskFiles['mask']):
            f = open(self.etpMaskFiles['mask'],"r")
            content = f.readlines()
            f.close()
            # filter comments and white lines
            content = [x.strip() for x in content if not x.startswith("#") and x.strip()]
            for line in content:
                data.add(line)
        return data

    def license_mask_parser(self):
        self.__validateEntropyCache(self.etpMaskFiles['license_mask'],self.etpMtimeFiles['license_mask_mtime'])

        data = set()
        if os.path.isfile(self.etpMaskFiles['license_mask']):
            f = open(self.etpMaskFiles['license_mask'],"r")
            content = f.readlines()
            f.close()
            # filter comments and white lines
            content = [x.strip() for x in content if not x.startswith("#") and x.strip()]
            for line in content:
                data.add(line)
        return data

    def repos_license_whitelist_parser(self):
        data = {}
        for repoid in self.etpMaskFiles['repos_license_whitelist']:
            data[repoid] = set()

            self.__validateEntropyCache(self.etpMaskFiles['repos_license_whitelist'][repoid],self.etpMtimeFiles['repos_license_whitelist'][repoid], repoid = repoid)

            if os.path.isfile(self.etpMaskFiles['repos_license_whitelist'][repoid]):
                f = open(self.etpMaskFiles['repos_license_whitelist'][repoid],"r")
                content = f.readlines()
                f.close()
                # filter comments and white lines
                content = [x.strip() for x in content if not x.startswith("#") and x.strip()]
                for mylicense in content:
                    data[repoid].add(mylicense)
        return data

    def repos_mask_parser(self):

        data = {}
        for repoid in self.etpMaskFiles['repos_mask']:

            data[repoid] = {}
            data[repoid]['branch'] = {}
            data[repoid]['*'] = set()

            self.__validateEntropyCache(self.etpMaskFiles['repos_mask'][repoid],self.etpMtimeFiles['repos_mask'][repoid], repoid = repoid)
            if os.path.isfile(self.etpMaskFiles['repos_mask'][repoid]):
                f = open(self.etpMaskFiles['repos_mask'][repoid],"r")
                content = f.readlines()
                f.close()
                # filter comments and white lines
                content = [x.strip() for x in content if not x.startswith("#") and x.strip() and len(x.split()) <= 2]
                for line in content:
                    line = line.split()
                    if len(line) == 1:
                        data[repoid]['*'].add(line[0])
                    else:
                        if not data[repoid]['branch'].has_key(line[1]):
                            data[repoid]['branch'][line[1]] = set()
                        data[repoid]['branch'][line[1]].add(line[0])
        return data

    '''
    internal functions
    '''

    def __removeRepoCache(self, repoid = None):
        if os.path.isdir(etpConst['dumpstoragedir']):
            if repoid:
                self.Entropy.repository_move_clear_cache(repoid)
            else:
                for repoid in etpRepositoriesOrder:
                    self.Entropy.repository_move_clear_cache(repoid)
        else:
            os.makedirs(etpConst['dumpstoragedir'])

    def __saveFileMtime(self,toread,tosaveinto):

        if not os.path.isfile(toread):
            currmtime = 0.0
        else:
            currmtime = os.path.getmtime(toread)

        if not os.path.isdir(etpConst['dumpstoragedir']):
            os.makedirs(etpConst['dumpstoragedir'],0775)
            const_setup_perms(etpConst['dumpstoragedir'],etpConst['entropygid'])

        f = open(tosaveinto,"w")
        f.write(str(currmtime))
        f.flush()
        f.close()
        os.chmod(tosaveinto,0664)
        if etpConst['entropygid'] != None:
            os.chown(tosaveinto,0,etpConst['entropygid'])


    def __validateEntropyCache(self, maskfile, mtimefile, repoid = None):

        if os.getuid() != 0: # can't validate if running as user, moreover users can't make changes, so...
            return

        # handle on-disk cache validation
        # in this case, repositories cache
        # if package.keywords is changed, we must destroy cache
        if not os.path.isfile(mtimefile):
            # we can't know if package.keywords has been updated
            # remove repositories caches
            self.__removeRepoCache(repoid = repoid)
            self.__saveFileMtime(maskfile,mtimefile)
        else:
            # check mtime
            try:
                f = open(mtimefile,"r")
                mtime = float(f.readline().strip())
                f.close()
                # compare with current mtime
                try:
                    currmtime = os.path.getmtime(maskfile)
                except OSError:
                    currmtime = 0.0
                if mtime != currmtime:
                    self.__removeRepoCache(repoid = repoid)
                    self.__saveFileMtime(maskfile,mtimefile)
            except:
                self.__removeRepoCache(repoid = repoid)
                self.__saveFileMtime(maskfile,mtimefile)

class Callable:
    def __init__(self, anycallable):
        self.__call__ = anycallable

class MultipartPostHandler(urllib2.BaseHandler):
    handler_order = urllib2.HTTPHandler.handler_order - 10 # needs to run first

    def http_request(self, request):

        import urllib
        doseq = 1

        data = request.get_data()
        if data is not None and type(data) != str:
            v_files = []
            v_vars = []
            try:
                 for(key, value) in data.items():
                     if type(value) == file:
                         v_files.append((key, value))
                     else:
                         v_vars.append((key, value))
            except TypeError:
                systype, value, traceback = sys.exc_info()
                raise TypeError, "not a valid non-string sequence or mapping object", traceback

            if len(v_files) == 0:
                data = urllib.urlencode(v_vars, doseq)
            else:
                boundary, data = self.multipart_encode(v_vars, v_files)

                contenttype = 'multipart/form-data; boundary=%s' % boundary
                '''
                if (request.has_header('Content-Type')
                   and request.get_header('Content-Type').find('multipart/form-data') != 0):
                    print "Replacing %s with %s" % (request.get_header('content-type'), 'multipart/form-data')
                '''
                request.add_unredirected_header('Content-Type', contenttype)
            request.add_data(data)
        return request

    def multipart_encode(vars, files, boundary = None, buf = None):

        from cStringIO import StringIO
        import mimetools, mimetypes

        if boundary is None:
            boundary = mimetools.choose_boundary()
        if buf is None:
            buf = StringIO()
        for(key, value) in vars:
            buf.write('--%s\r\n' % boundary)
            buf.write('Content-Disposition: form-data; name="%s"' % key)
            buf.write('\r\n\r\n' + value + '\r\n')
        for(key, fd) in files:
            file_size = os.fstat(fd.fileno())[stat.ST_SIZE]
            filename = fd.name.split('/')[-1]
            contenttype = mimetypes.guess_type(filename)[0] or 'application/octet-stream'
            buf.write('--%s\r\n' % boundary)
            buf.write('Content-Disposition: form-data; name="%s"; filename="%s"\r\n' % (key, filename))
            buf.write('Content-Type: %s\r\n' % contenttype)
            # buffer += 'Content-Length: %s\r\n' % file_size
            fd.seek(0)
            buf.write('\r\n' + fd.read() + '\r\n')
        buf.write('--' + boundary + '--\r\n\r\n')
        buf = buf.getvalue()
        return boundary, buf
    multipart_encode = Callable(multipart_encode)

    https_request = http_request

class ErrorReportInterface:

    def __init__(self, post_url = etpConst['handlers']['errorsend']):
        self.url = post_url
        self.opener = urllib2.build_opener(MultipartPostHandler)
        self.generated = False
        self.params = {}

        if etpConst['proxy']:
            proxy_support = urllib2.ProxyHandler(etpConst['proxy'])
            opener = urllib2.build_opener(proxy_support)
            urllib2.install_opener(opener)

    def prepare(self, tb_text, name, email, report_data = "", description = ""):
        self.params['arch'] = etpConst['currentarch']
        self.params['stacktrace'] = tb_text
        self.params['name'] = name
        self.params['email'] = email
        self.params['version'] = etpConst['entropyversion']
        self.params['errordata'] = report_data
        self.params['description'] = description
        self.params['arguments'] = ' '.join(sys.argv)
        self.params['uid'] = etpConst['uid']
        self.params['system_version'] = "N/A"
        self.params['processes'] = ''
        self.params['lspci'] = ''
        self.params['dmesg'] = ''
        if os.access(etpConst['systemreleasefile'],os.R_OK):
            f = open(etpConst['systemreleasefile'],"r")
            self.params['system_version'] = f.readlines()
            f.close()

        myprocesses = []
        try:
            myprocesses = commands.getoutput('ps auxf').split("\n")
        except:
            pass
        for line in myprocesses:
            mycount = 0
            for mychar in line:
                mycount += 1
                self.params['processes'] += mychar
                if mycount == 80:
                    self.params['processes'] += "\n"
                    mycount = 0
            if mycount != 0:
                self.params['processes'] += "\n"

        try:
            self.params['lspci'] = commands.getoutput('/usr/sbin/lspci')
            self.params['dmesg'] = commands.getoutput('dmesg')
        except:
            pass
        self.generated = True

    # params is a dict, key(HTTP post item name): value
    def submit(self):
        if self.generated:
            result = self.opener.open(self.url, self.params).read()
            if result.strip() == "1":
                return True
            return False
        else:
            mytxt = _("Not prepared yet")
            raise exceptionTools.PermissionDenied("PermissionDenied: %s" % (mytxt,))


'''
   ~~ GIVES YOU WINGS ~~
'''
class SecurityInterface:

    # thanks to Gentoo "gentoolkit" package, License below:

    # This program is licensed under the GPL, version 2

    # WARNING: this code is only tested by a few people and should NOT be used
    # on production systems at this stage. There are possible security holes and probably
    # bugs in this code. If you test it please report ANY success or failure to
    # me (genone@gentoo.org).

    # The following planned features are currently on hold:
    # - getting GLSAs from http/ftp servers (not really useful without the fixed ebuilds)
    # - GPG signing/verification (until key policy is clear)

    def __init__(self, EquoInstance):

        if not isinstance(EquoInstance,EquoInterface):
            mytxt = _("A valid EquoInterface instance or subclass is needed")
            raise exceptionTools.IncorrectParameter("IncorrectParameter: %s" % (mytxt,))
        self.Entropy = EquoInstance
        self.lastfetch = None
        self.previous_checksum = "0"
        self.advisories_changed = None
        self.adv_metadata = None
        self.affected_atoms = None

        from xml.dom import minidom
        self.minidom = minidom

        self.op_mappings = {
                            "le": "<=",
                            "lt": "<",
                            "eq": "=",
                            "gt": ">",
                            "ge": ">=",
                            "rge": ">=", # >=~
                            "rle": "<=", # <=~
                            "rgt": ">", # >~
                            "rlt": "<" # <~
        }

        self.unpackdir = os.path.join(etpConst['entropyunpackdir'],"security-"+str(self.Entropy.entropyTools.getRandomNumber()))
        self.security_url = etpConst['securityurl']
        self.unpacked_package = os.path.join(self.unpackdir,"glsa_package")
        self.security_url_checksum = etpConst['securityurl']+etpConst['packageshashfileext']
        self.download_package = os.path.join(self.unpackdir,os.path.basename(etpConst['securityurl']))
        self.download_package_checksum = self.download_package+etpConst['packageshashfileext']
        self.old_download_package_checksum = os.path.join(etpConst['dumpstoragedir'],os.path.basename(etpConst['securityurl']))+etpConst['packageshashfileext']

        self.security_package = os.path.join(etpConst['securitydir'],os.path.basename(etpConst['securityurl']))
        self.security_package_checksum = self.security_package+etpConst['packageshashfileext']

        try:
            if os.path.isfile(etpConst['securitydir']) or os.path.islink(etpConst['securitydir']):
                os.remove(etpConst['securitydir'])
            if not os.path.isdir(etpConst['securitydir']):
                os.makedirs(etpConst['securitydir'],0775)
        except OSError:
            pass
        const_setup_perms(etpConst['securitydir'],etpConst['entropygid'])

        if os.path.isfile(self.old_download_package_checksum):
            f = open(self.old_download_package_checksum)
            try:
                self.previous_checksum = f.readline().strip().split()[0]
            except:
                pass
            f.close()

    def __prepare_unpack(self):

        if os.path.isfile(self.unpackdir) or os.path.islink(self.unpackdir):
            os.remove(self.unpackdir)
        if os.path.isdir(self.unpackdir):
            shutil.rmtree(self.unpackdir,True)
            try:
                os.rmdir(self.unpackdir)
            except OSError:
                pass
        os.makedirs(self.unpackdir,0775)
        const_setup_perms(self.unpackdir,etpConst['entropygid'])

    def __download_glsa_package(self):
        return self.__generic_download(self.security_url, self.download_package)

    def __download_glsa_package_checksum(self):
        return self.__generic_download(self.security_url_checksum, self.download_package_checksum, showSpeed = False)

    def __generic_download(self, url, save_to, showSpeed = True):
        fetchConn = self.Entropy.urlFetcher(url, save_to, resume = False, showSpeed = showSpeed)
        fetchConn.progress = self.Entropy.progress
        rc = fetchConn.download()
        del fetchConn
        if rc in ("-1","-2","-3"):
            return False
        # setup permissions
        self.Entropy.setup_default_file_perms(save_to)
        return True

    def __verify_checksum(self):

        # read checksum
        if not os.path.isfile(self.download_package_checksum) or not os.access(self.download_package_checksum,os.R_OK):
            return 1

        f = open(self.download_package_checksum)
        try:
            checksum = f.readline().strip().split()[0]
            f.close()
        except:
            return 2

        if checksum == self.previous_checksum:
            self.advisories_changed = False
        else:
            self.advisories_changed = True
        md5res = self.Entropy.entropyTools.compareMd5(self.download_package,checksum)
        if not md5res:
            return 3
        return 0

    def __unpack_advisories(self):
        rc = self.Entropy.entropyTools.uncompressTarBz2(
                                                            self.download_package,
                                                            self.unpacked_package,
                                                            catchEmpty = True
                                                        )
        const_setup_perms(self.unpacked_package,etpConst['entropygid'])
        return rc

    def __clear_previous_advisories(self):
        if os.listdir(etpConst['securitydir']):
            shutil.rmtree(etpConst['securitydir'],True)
            if not os.path.isdir(etpConst['securitydir']):
                os.makedirs(etpConst['securitydir'],0775)
            const_setup_perms(self.unpackdir,etpConst['entropygid'])

    def __put_advisories_in_place(self):
        for advfile in os.listdir(self.unpacked_package):
            from_file = os.path.join(self.unpacked_package,advfile)
            to_file = os.path.join(etpConst['securitydir'],advfile)
            shutil.move(from_file,to_file)

    def __cleanup_garbage(self):
        shutil.rmtree(self.unpackdir,True)

    def clear(self, xcache = False):
        self.adv_metadata = None
        if xcache:
            self.Entropy.clear_dump_cache(etpCache['advisories'])

    def get_advisories_cache(self):

        if self.adv_metadata != None:
            return self.adv_metadata

        if self.Entropy.xcache:
            dir_checksum = self.Entropy.entropyTools.md5sum_directory(etpConst['securitydir'])
            c_hash = str(hash(etpConst['branch'])) + \
                     str(hash(dir_checksum)) + \
                     str(hash(etpConst['systemroot']))
            c_hash = str(hash(c_hash))
            adv_metadata = self.Entropy.dumpTools.loadobj(etpCache['advisories']+c_hash)
            if adv_metadata != None:
                self.adv_metadata = adv_metadata.copy()
                return self.adv_metadata

    def __set_advisories_cache(self, adv_metadata):
        if self.Entropy.xcache:
            dir_checksum = self.Entropy.entropyTools.md5sum_directory(etpConst['securitydir'])
            c_hash = str(hash(etpConst['branch'])) + \
                     str(hash(dir_checksum)) + \
                     str(hash(etpConst['systemroot']))
            c_hash = str(hash(c_hash))
            try:
                self.Entropy.dumpTools.dumpobj(etpCache['advisories']+c_hash,adv_metadata)
            except IOError:
                pass

    def get_advisories_list(self):
        if not self.check_advisories_availability():
            return []
        xmls = os.listdir(etpConst['securitydir'])
        xmls = [x for x in xmls if x.endswith(".xml") and x.startswith("glsa-")]
        xmls.sort()
        return xmls

    def get_advisories_metadata(self):

        cached = self.get_advisories_cache()
        if cached != None:
            return cached

        adv_metadata = {}
        xmls = self.get_advisories_list()
        maxlen = len(xmls)
        count = 0
        for xml in xmls:

            count += 1
            if not etpUi['quiet']: self.Entropy.updateProgress(":: "+str(round((float(count)/maxlen)*100,1))+"% ::", importance = 0, type = "info", back = True)

            xml_metadata = None
            exc_string = ""
            exc_err = ""
            try:
                xml_metadata = self.get_xml_metadata(xml)
            except KeyboardInterrupt:
                return {}
            except Exception, e:
                exc_string = str(Exception)
                exc_err = str(e)
            if xml_metadata == None:
                more_info = ""
                if exc_string:
                    mytxt = _("Error")
                    more_info = " %s: %s: %s" % (mytxt,exc_string,exc_err,)
                mytxt = "%s: %s: %s! %s" % (
                    blue(_("Warning")),
                    bold(xml),
                    blue(_("advisory broken")),
                    more_info,
                )
                self.Entropy.updateProgress(
                    mytxt,
                    importance = 1,
                    type = "warning",
                    header = red(" !!! ")
                )
                continue
            elif not xml_metadata:
                continue
            adv_metadata.update(xml_metadata)

        adv_metadata = self.filter_advisories(adv_metadata)
        self.__set_advisories_cache(adv_metadata)
        self.adv_metadata = adv_metadata.copy()
        return adv_metadata

    # this function filters advisories for packages that aren't
    # in the repositories. Note: only keys will be matched
    def filter_advisories(self, adv_metadata):
        keys = adv_metadata.keys()
        for key in keys:
            valid = True
            if adv_metadata[key]['affected']:
                affected = adv_metadata[key]['affected']
                affected_keys = affected.keys()
                valid = False
                skipping_keys = set()
                for a_key in affected_keys:
                    match = self.Entropy.atomMatch(a_key)
                    if match[0] != -1:
                        # it's in the repos, it's valid
                        valid = True
                    else:
                        skipping_keys.add(a_key)
                if not valid:
                    del adv_metadata[key]
                for a_key in skipping_keys:
                    try:
                        del adv_metadata[key]['affected'][a_key]
                    except KeyError:
                        pass
                try:
                    if not adv_metadata[key]['affected']:
                        del adv_metadata[key]
                except KeyError:
                    pass

        return adv_metadata

    def is_affected(self, adv_key, adv_data = {}):
        if not adv_data:
            adv_data = self.get_advisories_metadata()
        if adv_key not in adv_data:
            return False
        mydata = adv_data[adv_key].copy()
        del adv_data

        if not mydata['affected']:
            return False

        for key in mydata['affected']:

            vul_atoms = mydata['affected'][key][0]['vul_atoms']
            unaff_atoms = mydata['affected'][key][0]['unaff_atoms']
            unaffected_atoms = set()
            if not vul_atoms:
                return False
            # XXX: does multimatch work correctly?
            for atom in unaff_atoms:
                matches = self.Entropy.clientDbconn.atomMatch(atom, multiMatch = True)
                if matches[1] == 0:
                    for idpackage in matches[0]:
                        unaffected_atoms.add((idpackage,0))

            for atom in vul_atoms:
                match = self.Entropy.clientDbconn.atomMatch(atom)
                if (match[0] != -1) and (match not in unaffected_atoms):
                    if self.affected_atoms == None:
                        self.affected_atoms = set()
                    self.affected_atoms.add(atom)
                    return True
        return False

    def get_vulnerabilities(self):
        return self.get_affection()

    def get_fixed_vulnerabilities(self):
        return self.get_affection(affected = False)

    # if not affected: not affected packages will be returned
    # if affected: affected packages will be returned
    def get_affection(self, affected = True):
        adv_data = self.get_advisories_metadata()
        adv_data_keys = adv_data.keys()
        valid_keys = set()
        for adv in adv_data_keys:
            is_affected = self.is_affected(adv,adv_data)
            if affected == is_affected:
                valid_keys.add(adv)
        # we need to filter our adv_data and return
        for key in adv_data_keys:
            if key not in valid_keys:
                try:
                    del adv_data[key]
                except KeyError:
                    pass
        # now we need to filter packages in adv_dat
        for adv in adv_data:
            for key in adv_data[adv]['affected'].keys():
                atoms = adv_data[adv]['affected'][key][0]['vul_atoms']
                applicable = True
                for atom in atoms:
                    if atom in self.affected_atoms:
                        applicable = False
                        break
                if applicable == affected:
                    del adv_data[adv]['affected'][key]
        return adv_data

    def get_affected_atoms(self):
        adv_data = self.get_advisories_metadata()
        adv_data_keys = adv_data.keys()
        del adv_data
        self.affected_atoms = set()
        for key in adv_data_keys:
            self.is_affected(key)
        return self.affected_atoms

    def get_xml_metadata(self, xmlfilename):
        xml_data = {}
        xmlfile = os.path.join(etpConst['securitydir'],xmlfilename)
        try:
            xmldoc = self.minidom.parse(xmlfile)
        except:
            return None

        # get base data
        glsa_tree = xmldoc.getElementsByTagName("glsa")[0]
        glsa_product = glsa_tree.getElementsByTagName("product")[0]
        if glsa_product.getAttribute("type") != "ebuild":
            return {}

        glsa_id = glsa_tree.getAttribute("id")
        glsa_title = glsa_tree.getElementsByTagName("title")[0].firstChild.data
        glsa_synopsis = glsa_tree.getElementsByTagName("synopsis")[0].firstChild.data
        glsa_announced = glsa_tree.getElementsByTagName("announced")[0].firstChild.data
        glsa_revised = glsa_tree.getElementsByTagName("revised")[0].firstChild.data

        xml_data['filename'] = xmlfilename
        xml_data['url'] = "http://www.gentoo.org/security/en/glsa/%s" % (xmlfilename,)
        xml_data['title'] = glsa_title.strip()
        xml_data['synopsis'] = glsa_synopsis.strip()
        xml_data['announced'] = glsa_announced.strip()
        xml_data['revised'] = glsa_revised.strip()
        xml_data['bugs'] = ["https://bugs.gentoo.org/show_bug.cgi?id="+x.firstChild.data.strip() for x in glsa_tree.getElementsByTagName("bug")]
        xml_data['access'] = ""
        try:
            xml_data['access'] = glsa_tree.getElementsByTagName("access")[0].firstChild.data.strip()
        except IndexError:
            pass

        # references
        references = glsa_tree.getElementsByTagName("references")[0]
        xml_data['references'] = [x.getAttribute("link").strip() for x in references.getElementsByTagName("uri")]

        try:
            xml_data['description'] = ""
            xml_data['description_items'] = []
            desc = glsa_tree.getElementsByTagName("description")[0].getElementsByTagName("p")[0].firstChild.data.strip()
            xml_data['description'] = desc
            items = glsa_tree.getElementsByTagName("description")[0].getElementsByTagName("ul")
            for item in items:
                li_items = item.getElementsByTagName("li")
                for li_item in li_items:
                    xml_data['description_items'].append(' '.join([x.strip() for x in li_item.firstChild.data.strip().split("\n")]))
        except IndexError:
            xml_data['description'] = ""
            xml_data['description_items'] = []
        try:
            workaround = glsa_tree.getElementsByTagName("workaround")[0]
            xml_data['workaround'] = workaround.getElementsByTagName("p")[0].firstChild.data.strip()
        except IndexError:
            xml_data['workaround'] = ""

        try:
            xml_data['resolution'] = []
            resolution = glsa_tree.getElementsByTagName("resolution")[0]
            p_elements = resolution.getElementsByTagName("p")
            for p_elem in p_elements:
                xml_data['resolution'].append(p_elem.firstChild.data.strip())
        except IndexError:
            xml_data['resolution'] = []

        try:
            impact = glsa_tree.getElementsByTagName("impact")[0]
            xml_data['impact'] = impact.getElementsByTagName("p")[0].firstChild.data.strip()
        except IndexError:
            xml_data['impact'] = ""
        xml_data['impacttype'] = glsa_tree.getElementsByTagName("impact")[0].getAttribute("type").strip()

        try:
            background = glsa_tree.getElementsByTagName("background")[0]
            xml_data['background'] = background.getElementsByTagName("p")[0].firstChild.data.strip()
        except IndexError:
            xml_data['background'] = ""

        # affection information
        affected = glsa_tree.getElementsByTagName("affected")[0]
        affected_packages = {}
        # we will then filter affected_packages using repositories information
        # if not affected_packages: advisory will be dropped
        for p in affected.getElementsByTagName("package"):
            name = p.getAttribute("name")
            if not affected_packages.has_key(name):
                affected_packages[name] = []

            pdata = {}
            pdata["arch"] = p.getAttribute("arch").strip()
            pdata["auto"] = (p.getAttribute("auto") == "yes")
            pdata["vul_vers"] = [self.__make_version(v) for v in p.getElementsByTagName("vulnerable")]
            pdata["unaff_vers"] = [self.__make_version(v) for v in p.getElementsByTagName("unaffected")]
            pdata["vul_atoms"] = [self.__make_atom(name, v) for v in p.getElementsByTagName("vulnerable")]
            pdata["unaff_atoms"] = [self.__make_atom(name, v) for v in p.getElementsByTagName("unaffected")]
            affected_packages[name].append(pdata)
        xml_data['affected'] = affected_packages.copy()

        return {glsa_id: xml_data}

    def __make_version(self, vnode):
        """
        creates from the information in the I{versionNode} a 
        version string (format <op><version>).

        @type	vnode: xml.dom.Node
        @param	vnode: a <vulnerable> or <unaffected> Node that
                                                    contains the version information for this atom
        @rtype:		String
        @return:	the version string
        """
        return self.op_mappings[vnode.getAttribute("range")] + vnode.firstChild.data.strip()

    def __make_atom(self, pkgname, vnode):
        """
        creates from the given package name and information in the 
        I{versionNode} a (syntactical) valid portage atom.

        @type	pkgname: String
        @param	pkgname: the name of the package for this atom
        @type	vnode: xml.dom.Node
        @param	vnode: a <vulnerable> or <unaffected> Node that
                                                    contains the version information for this atom
        @rtype:		String
        @return:	the portage atom
        """
        return str(self.op_mappings[vnode.getAttribute("range")] + pkgname + "-" + vnode.firstChild.data.strip())

    def check_advisories_availability(self):
        if not os.path.lexists(etpConst['securitydir']):
            return False
        if not os.path.isdir(etpConst['securitydir']):
            return False
        else:
            return True
        return False

    def fetch_advisories(self):

        mytxt = "%s: %s" % (bold(_("Security Advisories")),blue(_("testing service connection")),)
        self.Entropy.updateProgress(
            mytxt,
            importance = 2,
            type = "info",
            header = red(" @@ "),
            footer = red(" ...")
        )

        # Test network connectivity
        conntest = self.Entropy.entropyTools.get_remote_data(etpConst['conntestlink'])
        if not conntest:
            mytxt = _("Cannot connect to %s") % (etpConst['conntestlink'],)
            raise exceptionTools.OnlineMirrorError("OnlineMirrorError: %s" % (mytxt,))

        mytxt = "%s: %s %s" % (bold(_("Security Advisories")),blue(_("getting latest GLSAs")),red("..."),)
        self.Entropy.updateProgress(
            mytxt,
            importance = 2,
            type = "info",
            header = red(" @@ ")
        )

        gave_up = self.Entropy.lock_check(self.Entropy._resources_run_check_lock)
        if gave_up:
            return 7

        locked = self.Entropy.application_lock_check()
        if locked:
            self.Entropy._resources_run_remove_lock()
            return 4

        # lock
        self.Entropy._resources_run_create_lock()
        try:
            rc = self.run_fetch()
        except:
            self.Entropy._resources_run_remove_lock()
            raise
        if rc != 0: return rc

        self.Entropy._resources_run_remove_lock()

        if self.advisories_changed:
            advtext = "%s: %s" % (bold(_("Security Advisories")),darkgreen(_("updated successfully")),)
        else:
            advtext = "%s: %s" % (bold(_("Security Advisories")),darkgreen(_("already up to date")),)

        self.Entropy.updateProgress(
            advtext,
            importance = 2,
            type = "info",
            header = red(" @@ ")
        )

        return 0

    def run_fetch(self):
        # prepare directories
        self.__prepare_unpack()

        # download package
        status = self.__download_glsa_package()
        self.lastfetch = status
        if not status:
            mytxt = "%s: %s." % (bold(_("Security Advisories")),darkred(_("unable to download the package, sorry")),)
            self.Entropy.updateProgress(
                mytxt,
                importance = 2,
                type = "error",
                header = red("   ## ")
            )
            self.Entropy._resources_run_remove_lock()
            return 1

        mytxt = "%s: %s %s" % (bold(_("Security Advisories")),blue(_("Verifying checksum")),red("..."),)
        self.Entropy.updateProgress(
            mytxt,
            importance = 1,
            type = "info",
            header = red("   # "),
            back = True
        )

        # download digest
        status = self.__download_glsa_package_checksum()
        if not status:
            mytxt = "%s: %s." % (bold(_("Security Advisories")),darkred(_("cannot download the checksum, sorry")),)
            self.Entropy.updateProgress(
                mytxt,
                importance = 2,
                type = "error",
                header = red("   ## ")
            )
            self.Entropy._resources_run_remove_lock()
            return 2

        # verify digest
        status = self.__verify_checksum()

        if status == 1:
            mytxt = "%s: %s." % (bold(_("Security Advisories")),darkred(_("cannot open packages, sorry")),)
            self.Entropy.updateProgress(
                mytxt,
                importance = 2,
                type = "error",
                header = red("   ## ")
            )
            self.Entropy._resources_run_remove_lock()
            return 3
        elif status == 2:
            mytxt = "%s: %s." % (bold(_("Security Advisories")),darkred(_("cannot read the checksum, sorry")),)
            self.Entropy.updateProgress(
                mytxt,
                importance = 2,
                type = "error",
                header = red("   ## ")
            )
            self.Entropy._resources_run_remove_lock()
            return 4
        elif status == 3:
            mytxt = "%s: %s." % (bold(_("Security Advisories")),darkred(_("digest verification failed, sorry")),)
            self.Entropy.updateProgress(
                mytxt,
                importance = 2,
                type = "error",
                header = red("   ## ")
            )
            self.Entropy._resources_run_remove_lock()
            return 5
        elif status == 0:
            mytxt = "%s: %s." % (bold(_("Security Advisories")),darkgreen(_("verification Successful")),)
            self.Entropy.updateProgress(
                mytxt,
                importance = 1,
                type = "info",
                header = red("   # ")
            )
        else:
            mytxt = _("Return status not valid")
            raise exceptionTools.InvalidData("InvalidData: %s." % (mytxt,))

        # save downloaded md5
        if os.path.isfile(self.download_package_checksum) and os.path.isdir(etpConst['dumpstoragedir']):
            if os.path.isfile(self.old_download_package_checksum):
                os.remove(self.old_download_package_checksum)
            shutil.copy2(self.download_package_checksum,self.old_download_package_checksum)
            self.Entropy.setup_default_file_perms(self.old_download_package_checksum)

        # now unpack in place
        status = self.__unpack_advisories()
        if status != 0:
            mytxt = "%s: %s." % (bold(_("Security Advisories")),darkred(_("digest verification failed, try again later")),)
            self.Entropy.updateProgress(
                mytxt,
                importance = 2,
                type = "error",
                header = red("   ## ")
            )
            self.Entropy._resources_run_remove_lock()
            return 6

        mytxt = "%s: %s %s" % (bold(_("Security Advisories")),blue(_("installing")),red("..."),)
        self.Entropy.updateProgress(
            mytxt,
            importance = 1,
            type = "info",
            header = red("   # ")
        )

        # clear previous
        self.__clear_previous_advisories()
        # copy over
        self.__put_advisories_in_place()
        # remove temp stuff
        self.__cleanup_garbage()
        return 0

class SpmInterface:

    def __init__(self, OutputInterface):
        if not isinstance(OutputInterface, (EquoInterface, TextInterface, ServerInterface)):
                if OutputInterface == None:
                    OutputInterface = TextInterface()
                else:
                    mytxt = _("A valid TextInterface based instance is needed")
                    raise exceptionTools.IncorrectParameter(
                            "IncorrectParameter: %s" % (mytxt,)
                    )

        self.spm_backend = etpConst['spm']['backend']
        self.valid_backends = etpConst['spm']['available_backends']
        if self.spm_backend not in self.valid_backends:
            mytxt = "%s: %s" % (_("Invalid backend"),self.spm_backend,)
            raise exceptionTools.IncorrectParameter("IncorrectParameter: %s" % (mytxt,))

        if self.spm_backend == "portage":
            self.intf = PortageInterface(OutputInterface)

    @staticmethod
    def get_spm_interface():
        backend = etpConst['spm']['backend']
        if backend == "portage":
            return PortageInterface

class PortageInterface:

    import entropyTools

    class paren_normalize(list):
        """Take a dependency structure as returned by paren_reduce or use_reduce
        and generate an equivalent structure that has no redundant lists."""
        def __init__(self, src):
            list.__init__(self)
            self._zap_parens(src, self)

        def _zap_parens(self, src, dest, disjunction=False):
            if not src:
                return dest
            i = iter(src)
            for x in i:
                if isinstance(x, basestring):
                    if x == '||':
                        x = self._zap_parens(i.next(), [], disjunction=True)
                        if len(x) == 1:
                            dest.append(x[0])
                        else:
                            dest.append("||")
                            dest.append(x)
                    elif x.endswith("?"):
                        dest.append(x)
                        dest.append(self._zap_parens(i.next(), []))
                    else:
                        dest.append(x)
                else:
                    if disjunction:
                        x = self._zap_parens(x, [])
                        if len(x) == 1:
                            dest.append(x[0])
                        else:
                            dest.append(x)
                    else:
                        self._zap_parens(x, dest)
            return dest

    def __init__(self, OutputInterface):
        if not isinstance(OutputInterface, (EquoInterface, TextInterface, ServerInterface)):
            mytxt = _("A valid TextInterface based instance is needed")
            raise exceptionTools.IncorrectParameter("IncorrectParameter: %s" % (mytxt,))

        # interface only needed OutputInterface functions
        self.updateProgress = OutputInterface.updateProgress
        self.askQuestion = OutputInterface.askQuestion

        # importing portage stuff
        import portage
        self.portage = portage
        try:
            import portage_const
        except ImportError:
            import portage.const as portage_const
        self.portage_const = portage_const

    def run_fixpackages(self, myroot = None):
        if myroot == None:
            myroot = etpConst['systemroot']+"/"
        mydb = {}
        mydb[myroot] = {}
        mydb[myroot]['vartree'] = self._get_portage_vartree(myroot)
        mydb[myroot]['porttree'] = self._get_portage_portagetree(myroot)
        mydb[myroot]['bintree'] = self._get_portage_binarytree(myroot)
        mydb[myroot]['virtuals'] = self.portage.settings.getvirtuals(myroot)
        self.portage._global_updates(mydb, {}) # always force

    def get_third_party_mirrors(self, mirrorname):
        x = []
        if self.portage.thirdpartymirrors.has_key(mirrorname):
            x = self.portage.thirdpartymirrors[mirrorname]
        return x

    def get_spm_setting(self, var):
        return self.portage.settings[var]

    # Packages in system (in the Portage language -> emerge system, remember?)
    def get_atoms_in_system(self):
        system = self.portage.settings.packages
        sysoutput = []
        for x in system:
            y = self.get_installed_atoms(x)
            if (y != None):
                for z in y:
                    sysoutput.append(z)
        sysoutput.extend(etpConst['spm']['system_packages']) # add our packages
        return sysoutput

    def get_category_description_data(self, category):
        from xml.dom import minidom
        data = {}
        portdir = self.portage.settings['PORTDIR']
        myfile = os.path.join(portdir,category,"metadata.xml")
        if os.access(myfile,os.R_OK) and os.path.isfile(myfile):
            doc = minidom.parse(myfile)
            longdescs = doc.getElementsByTagName("longdescription")
            for longdesc in longdescs:
                data[longdesc.getAttribute("lang").strip()] = ' '.join([x.strip() for x in longdesc.firstChild.data.strip().split("\n")])
        return data

    def get_config_protect_and_mask(self):
        config_protect = self.portage.settings['CONFIG_PROTECT']
        config_protect = config_protect.split()
        config_protect_mask = self.portage.settings['CONFIG_PROTECT_MASK']
        config_protect_mask = config_protect_mask.split()
        # explode
        protect = []
        for x in config_protect:
            x = os.path.expandvars(x)
            protect.append(x)
        mask = []
        for x in config_protect_mask:
            x = os.path.expandvars(x)
            mask.append(x)
        return ' '.join(protect),' '.join(mask)

    # resolve atoms automagically (best, not current!)
    # sys-libs/application --> sys-libs/application-1.2.3-r1
    def get_best_atom(self, atom, match = "bestmatch-visible"):
        try:
            return self.portage.portdb.xmatch(match,str(atom))
        except ValueError:
            return None

    # same as above but includes masked ebuilds
    def get_best_masked_atom(self, atom):
        atoms = self.portage.portdb.xmatch("match-all",str(atom))
        # find the best
        try:
            from portage_versions import best
        except ImportError:
            from portage.versions import best
        return best(atoms)

    def get_atom_category(self, atom):
        try:
            return self.portage.portdb.xmatch("match-all",str(atom))[0].split("/")[0]
        except:
            return None

    def _get_portage_vartree(self, root):

        if not etpConst['spm']['cache'].has_key('portage'):
            etpConst['spm']['cache']['portage'] = {}
        if not etpConst['spm']['cache']['portage'].has_key('vartree'):
            etpConst['spm']['cache']['portage']['vartree'] = {}

        cached = etpConst['spm']['cache']['portage']['vartree'].get(root)
        if cached != None:
            return cached

        mytree = self.portage.vartree(root=root)
        etpConst['spm']['cache']['portage']['vartree'][root] = mytree
        return mytree

    def _get_portage_portagetree(self, root):

        if not etpConst['spm']['cache'].has_key('portage'):
            etpConst['spm']['cache']['portage'] = {}
        if not etpConst['spm']['cache']['portage'].has_key('portagetree'):
            etpConst['spm']['cache']['portage']['portagetree'] = {}

        cached = etpConst['spm']['cache']['portage']['portagetree'].get(root)
        if cached != None:
            return cached

        mytree = self.portage.portagetree(root=root)
        etpConst['spm']['cache']['portage']['portagetree'][root] = mytree
        return mytree

    def _get_portage_binarytree(self, root):

        if not etpConst['spm']['cache'].has_key('portage'):
            etpConst['spm']['cache']['portage'] = {}
        if not etpConst['spm']['cache']['portage'].has_key('binarytree'):
            etpConst['spm']['cache']['portage']['binarytree'] = {}

        cached = etpConst['spm']['cache']['portage']['binarytree'].get(root)
        if cached != None:
            return cached

        pkgdir = root+self.portage.settings['PKGDIR']
        mytree = self.portage.binarytree(root,pkgdir)
        etpConst['spm']['cache']['portage']['binarytree'][root] = mytree
        return mytree

    def _get_portage_config(self, config_root, root):

        if not etpConst['spm']['cache'].has_key('portage'):
            etpConst['spm']['cache']['portage'] = {}
        if not etpConst['spm']['cache']['portage'].has_key('config'):
            etpConst['spm']['cache']['portage']['config'] = {}

        cached = etpConst['spm']['cache']['portage']['config'].get((config_root,root))
        if cached != None:
            return cached

        mysettings = self.portage.config(config_root = config_root, target_root = root, config_incrementals = self.portage_const.INCREMENTALS)
        etpConst['spm']['cache']['portage']['config'][(config_root,root)] = mysettings
        return mysettings

    # please always force =pkgcat/pkgname-ver if possible
    def get_installed_atom(self, atom):
        mypath = etpConst['systemroot']+"/"
        mytree = self._get_portage_vartree(mypath)
        rc = mytree.dep_match(str(atom))
        if rc:
            return rc[-1]
        return None

    def get_package_slot(self, atom):
        mypath = etpConst['systemroot']+"/"
        mytree = self._get_portage_vartree(mypath)
        if atom.startswith("="):
            atom = atom[1:]
        rc = mytree.getslot(atom)
        if rc:
            return rc
        return None

    def get_installed_atoms(self, atom):
        mypath = etpConst['systemroot']+"/"
        mytree = self._get_portage_vartree(mypath)
        rc = mytree.dep_match(str(atom))
        if rc:
            return rc
        return None

    def search_keys(self, key):
        key_split = key.split("/")
        cat = key_split[0]
        name = key_split[1]
        cat_dir = os.path.join(self.get_vdb_path(),cat)
        if not os.path.isdir(cat_dir):
            return None
        dir_content = [os.path.join(cat,x) for x in os.listdir(cat_dir) if x.startswith(name)]
        if not dir_content:
            return None
        return dir_content

    # create a .tbz2 file in the specified path
    def quickpkg(self, atom, dirpath):

        # getting package info
        pkgname = atom.split("/")[1]
        pkgcat = atom.split("/")[0]
        #pkgfile = pkgname+".tbz2"
        if not os.path.isdir(dirpath):
            os.makedirs(dirpath)
        dirpath += "/"+pkgname+etpConst['packagesext']
        dbdir = self.get_vdb_path()+"/"+pkgcat+"/"+pkgname+"/"

        import tarfile
        import stat
        trees = self.portage.db["/"]
        vartree = trees["vartree"]
        dblnk = self.portage.dblink(pkgcat, pkgname, "/", vartree.settings, treetype="vartree", vartree=vartree)
        dblnk.lockdb()
        tar = tarfile.open(dirpath,"w:bz2")

        contents = dblnk.getcontents()
        id_strings = {}
        paths = contents.keys()
        paths.sort()

        for path in paths:
            try:
                exist = os.lstat(path)
            except OSError:
                continue # skip file
            ftype = contents[path][0]
            lpath = path
            arcname = path[1:]
            if 'dir' == ftype and \
                not stat.S_ISDIR(exist.st_mode) and \
                os.path.isdir(lpath):
                lpath = os.path.realpath(lpath)
            tarinfo = tar.gettarinfo(lpath, arcname)
            tarinfo.uname = id_strings.setdefault(tarinfo.uid, str(tarinfo.uid))
            tarinfo.gname = id_strings.setdefault(tarinfo.gid, str(tarinfo.gid))

            if stat.S_ISREG(exist.st_mode):
                tarinfo.type = tarfile.REGTYPE
                f = open(path)
                try:
                    tar.addfile(tarinfo, f)
                finally:
                    f.close()
            else:
                tar.addfile(tarinfo)

        tar.close()

        # appending xpak informations
        import etpXpak
        tbz2 = etpXpak.tbz2(dirpath)
        tbz2.recompose(dbdir)

        dblnk.unlockdb()

        if os.path.isfile(dirpath):
            return dirpath
        else:
            raise exceptionTools.FileNotFound("FileNotFound: Spm:quickpkg %s: %s %s" % (
                    _("error"),
                    dirpath,
                    _("not found"),
                )
            )

    def get_useflags(self):
        return self.portage.settings['USE']

    def get_useflags_force(self):
        return self.portage.settings.useforce

    def get_useflags_mask(self):
        return self.portage.settings.usemask

    def get_package_setting(self, atom, setting):
        myatom = atom[:]
        if myatom.startswith("="):
            myatom = myatom[1:]
        return self.portage.portdb.aux_get(myatom,[setting])[0]

    def query_files(self, atom):
        mypath = etpConst['systemroot']+"/"
        mysplit = atom.split("/")
        content = self.portage.dblink(mysplit[0], mysplit[1], mypath, self.portage.settings).getcontents()
        return content.keys()

    def query_belongs(self, filename, like = False):
        mypath = etpConst['systemroot']+"/"
        mytree = self._get_portage_vartree(mypath)
        packages = mytree.dbapi.cpv_all()
        matches = set()
        for package in packages:
            mysplit = package.split("/")
            content = self.portage.dblink(mysplit[0], mysplit[1], mypath, self.portage.settings).getcontents()
            if not like:
                if filename in content:
                    matches.add(package)
            else:
                for myfile in content:
                    if myfile.find(filename) != -1:
                        matches.add(package)
        return matches

    def calculate_dependencies(self, my_iuse, my_use, my_license, my_depend, my_rdepend, my_pdepend, my_provide, my_src_uri):
        metadata = {}
        metadata['USE'] = my_use
        metadata['IUSE'] = my_iuse
        metadata['LICENSE'] = my_license
        metadata['DEPEND'] = my_depend
        metadata['PDEPEND'] = my_pdepend
        metadata['RDEPEND'] = my_rdepend
        metadata['PROVIDE'] = my_provide
        metadata['SRC_URI'] = my_src_uri
        use = metadata['USE'].split()
        raw_use = use
        iuse = set(metadata['IUSE'].split())
        use = [f for f in use if f in iuse]
        use.sort()
        metadata['USE'] = " ".join(use)
        for k in "LICENSE", "RDEPEND", "DEPEND", "PDEPEND", "PROVIDE", "SRC_URI":
            try:
                deps = self.paren_reduce(metadata[k])
                deps = self.use_reduce(deps, uselist=raw_use)
                deps = self.paren_normalize(deps)
                if k == "LICENSE":
                    deps = self.paren_license_choose(deps)
                else:
                    deps = self.paren_choose(deps)
                deps = ' '.join(deps)
            except Exception, e:
                self.entropyTools.printTraceback()
                self.updateProgress(
                    darkred("%s: %s: %s :: %s") % (
                        _("Error calculating dependencies"),
                        str(Exception),
                        k,
                        e,
                    ),
                    importance = 1,
                    type = "error",
                    header = red(" !!! ")
                )
                deps = ''
                continue
            metadata[k] = deps
        return metadata

    def paren_reduce(self, mystr,tokenize=1):
        """

            # deps.py -- Portage dependency resolution functions
            # Copyright 2003-2004 Gentoo Foundation
            # Distributed under the terms of the GNU General Public License v2
            # $Id: portage_dep.py 9174 2008-01-11 05:49:02Z zmedico $

        Take a string and convert all paren enclosed entities into sublists, optionally
        futher splitting the list elements by spaces.

        Example usage:
                >>> paren_reduce('foobar foo ( bar baz )',1)
                ['foobar', 'foo', ['bar', 'baz']]
                >>> paren_reduce('foobar foo ( bar baz )',0)
                ['foobar foo ', [' bar baz ']]

        @param mystr: The string to reduce
        @type mystr: String
        @param tokenize: Split on spaces to produces further list breakdown
        @type tokenize: Integer
        @rtype: Array
        @return: The reduced string in an array
        """
        mylist = []
        while mystr:
            left_paren = mystr.find("(")
            has_left_paren = left_paren != -1
            right_paren = mystr.find(")")
            has_right_paren = right_paren != -1
            if not has_left_paren and not has_right_paren:
                freesec = mystr
                subsec = None
                tail = ""
            elif mystr[0] == ")":
                return [mylist,mystr[1:]]
            elif has_left_paren and not has_right_paren:
                raise exceptionTools.InvalidDependString(
                        "InvalidDependString: %s: '%s'" % (_("missing right parenthesis"),mystr,))
            elif has_left_paren and left_paren < right_paren:
                freesec,subsec = mystr.split("(",1)
                subsec,tail = self.paren_reduce(subsec,tokenize)
            else:
                subsec,tail = mystr.split(")",1)
                if tokenize:
                    subsec = self.strip_empty(subsec.split(" "))
                    return [mylist+subsec,tail]
                return mylist+[subsec],tail
            mystr = tail
            if freesec:
                if tokenize:
                    mylist = mylist + self.strip_empty(freesec.split(" "))
                else:
                    mylist = mylist + [freesec]
            if subsec is not None:
                mylist = mylist + [subsec]
        return mylist

    def strip_empty(self, myarr):
        """

            # deps.py -- Portage dependency resolution functions
            # Copyright 2003-2004 Gentoo Foundation
            # Distributed under the terms of the GNU General Public License v2
            # $Id: portage_dep.py 9174 2008-01-11 05:49:02Z zmedico $

        Strip all empty elements from an array

        @param myarr: The list of elements
        @type myarr: List
        @rtype: Array
        @return: The array with empty elements removed
        """
        for x in range(len(myarr)-1, -1, -1):
                if not myarr[x]:
                        del myarr[x]
        return myarr

    def use_reduce(self, deparray, uselist=[], masklist=[], matchall=0, excludeall=[]):
        """

            # deps.py -- Portage dependency resolution functions
            # Copyright 2003-2004 Gentoo Foundation
            # Distributed under the terms of the GNU General Public License v2
            # $Id: portage_dep.py 9174 2008-01-11 05:49:02Z zmedico $

        Takes a paren_reduce'd array and reduces the use? conditionals out
        leaving an array with subarrays

        @param deparray: paren_reduce'd list of deps
        @type deparray: List
        @param uselist: List of use flags
        @type uselist: List
        @param masklist: List of masked flags
        @type masklist: List
        @param matchall: Resolve all conditional deps unconditionally.  Used by repoman
        @type matchall: Integer
        @rtype: List
        @return: The use reduced depend array
        """
        # Quick validity checks
        for x in range(len(deparray)):
            if deparray[x] in ["||","&&"]:
                if len(deparray) - 1 == x or not isinstance(deparray[x+1], list):
                    mytxt = _("missing atom list in")
                    raise exceptionTools.InvalidDependString(deparray[x]+" "+mytxt+" \""+str(deparray)+"\"")
        if deparray and deparray[-1] and deparray[-1][-1] == "?":
            mytxt = _("Conditional without target in")
            raise exceptionTools.InvalidDependString("InvalidDependString: "+mytxt+" \""+str(deparray)+"\"")

        # This is just for use by emerge so that it can enable a backward compatibility
        # mode in order to gracefully deal with installed packages that have invalid
        # atoms or dep syntax.  For backward compatibility with api consumers, strict
        # behavior will be explicitly enabled as necessary.
        _dep_check_strict = False

        mydeparray = deparray[:]
        rlist = []
        while mydeparray:
            head = mydeparray.pop(0)

            if isinstance(head,list):
                additions = self.use_reduce(head, uselist, masklist, matchall, excludeall)
                if additions:
                    rlist.append(additions)
                elif rlist and rlist[-1] == "||":
                    #XXX: Currently some DEPEND strings have || lists without default atoms.
                    #	raise portage_exception.InvalidDependString("No default atom(s) in \""+paren_enclose(deparray)+"\"")
                    rlist.append([])
            else:
                if head[-1] == "?": # Use reduce next group on fail.
                    # Pull any other use conditions and the following atom or list into a separate array
                    newdeparray = [head]
                    while isinstance(newdeparray[-1], str) and newdeparray[-1][-1] == "?":
                        if mydeparray:
                            newdeparray.append(mydeparray.pop(0))
                        else:
                            raise ValueError, _("Conditional with no target")

                    # Deprecation checks
                    warned = 0
                    if len(newdeparray[-1]) == 0:
                        mytxt = "%s. (%s)" % (_("Empty target in string"),_("Deprecated"),)
                        self.updateProgress(
                            darkred("PortageInterface.use_reduce(): %s" % (mytxt,)),
                            importance = 0,
                            type = "error",
                            header = bold(" !!! ")
                        )
                        warned = 1
                    if len(newdeparray) != 2:
                        mytxt = "%s. (%s)" % (_("Nested use flags without parenthesis"),_("Deprecated"),)
                        self.updateProgress(
                            darkred("PortageInterface.use_reduce(): %s" % (mytxt,)),
                            importance = 0,
                            type = "error",
                            header = bold(" !!! ")
                        )
                        warned = 1
                    if warned:
                        self.updateProgress(
                            darkred("PortageInterface.use_reduce(): "+" ".join(map(str,[head]+newdeparray))),
                            importance = 0,
                            type = "error",
                            header = bold(" !!! ")
                        )

                    # Check that each flag matches
                    ismatch = True
                    missing_flag = False
                    for head in newdeparray[:-1]:
                        head = head[:-1]
                        if not head:
                            missing_flag = True
                            break
                        if head.startswith("!"):
                            head_key = head[1:]
                            if not head_key:
                                missing_flag = True
                                break
                            if not matchall and head_key in uselist or \
                                head_key in excludeall:
                                ismatch = False
                                break
                        elif head not in masklist:
                            if not matchall and head not in uselist:
                                    ismatch = False
                                    break
                        else:
                            ismatch = False
                    if missing_flag:
                        mytxt = _("Conditional without flag")
                        raise exceptionTools.InvalidDependString(
                                "InvalidDependString: "+mytxt+": \"" + \
                                str([head+"?", newdeparray[-1]])+"\"")

                    # If they all match, process the target
                    if ismatch:
                        target = newdeparray[-1]
                        if isinstance(target, list):
                            additions = self.use_reduce(target, uselist, masklist, matchall, excludeall)
                            if additions:
                                    rlist.append(additions)
                        elif not _dep_check_strict:
                            # The old deprecated behavior.
                            rlist.append(target)
                        else:
                            mytxt = _("Conditional without parenthesis")
                            raise exceptionTools.InvalidDependString(
                                    "InvalidDependString: "+mytxt+": '%s?'" % head)

                else:
                    rlist += [head]
        return rlist

    def paren_choose(self, dep_list):
        newlist = []
        do_skip = False
        for idx in range(len(dep_list)):

            if do_skip:
                do_skip = False
                continue

            item = dep_list[idx]
            if item == "||": # or
                next_item = dep_list[idx+1]
                if not next_item: # || ( asd? ( atom ) dsa? ( atom ) ) => [] if use asd and dsa are disabled
                    do_skip = True
                    continue
                item = self.dep_or_select(next_item) # must be a list
                if not item:
                    # no matches, transform to string and append, so reagent will fail
                    newlist.append(str(next_item))
                else:
                    newlist += item
                do_skip = True
            elif isinstance(item, list): # and
                item = self.dep_and_select(item)
                newlist += item
            else:
                newlist.append(item)

        return newlist

    def dep_and_select(self, and_list):
        do_skip = False
        newlist = []
        for idx in range(len(and_list)):

            if do_skip:
                do_skip = False
                continue

            x = and_list[idx]
            if x == "||":
                x = self.dep_or_select(and_list[idx+1])
                do_skip = True
                if not x:
                    x = str(and_list[idx+1])
                else:
                    newlist += x
            elif isinstance(x, list):
                x = self.dep_and_select(x)
                newlist += x
            else:
                newlist.append(x)

        # now verify if all are satisfied
        for x in newlist:
            match = self.get_installed_atom(x)
            if match == None:
                return []

        return newlist

    def dep_or_select(self, or_list):
        do_skip = False
        for idx in range(len(or_list)):
            if do_skip:
                do_skip = False
                continue
            x = or_list[idx]
            if x == "||": # or
                x = self.dep_or_select(or_list[idx+1])
                do_skip = True
            elif isinstance(x, list): # and
                x = self.dep_and_select(x)
                if not x:
                    continue
                # found
                return x
            else:
                x = [x]

            for y in x:
                match = self.get_installed_atom(y)
                if match != None:
                    return [y]

        return []

    def paren_license_choose(self, dep_list):

        newlist = set()
        for item in dep_list:

            if isinstance(item, list):
                # match the first
                data = set(self.paren_license_choose(item))
                newlist.update(data)
            else:
                if item not in ["||"]:
                    newlist.add(item)

        return list(newlist)

    def get_vdb_path(self):
        rc = etpConst['systemroot']+"/"+self.portage_const.VDB_PATH
        if (not rc.endswith("/")):
            return rc+"/"
        return rc

    def get_available_packages(self, categories = [], filter_reinstalls = True):
        mypath = etpConst['systemroot']+"/"
        mysettings = self._get_portage_config("/",mypath)
        portdb = self.portage.portdbapi(mysettings["PORTDIR"], mysettings = mysettings)
        cps = portdb.cp_all()
        visibles = set()
        for cp in cps:
            if categories and cp.split("/")[0] not in categories:
                continue
            # get slots
            slots = set()
            atoms = self.get_best_atom(cp, "match-visible")
            if atoms:
                for atom in atoms:
                    slots.add(portdb.aux_get(atom, ["SLOT"])[0])
                for slot in slots:
                    visibles.add(cp+":"+slot)
        del cps

        # now match visibles
        available = set()
        for visible in visibles:
            match = self.get_best_atom(visible)
            if match == None:
                continue
            if filter_reinstalls:
                installed = self.get_installed_atom(visible)
                # if not installed, installed == None
                if installed != match:
                    available.add(match)
            else:
                available.add(match)
        del visibles

        return available

    # Collect installed packages
    def get_installed_packages(self, dbdir = None):
        if not dbdir:
            appDbDir = self.get_vdb_path()
        else:
            appDbDir = dbdir
        dbDirs = os.listdir(appDbDir)
        installedAtoms = set()
        for pkgsdir in dbDirs:
            if os.path.isdir(appDbDir+pkgsdir):
                pkgdir = os.listdir(appDbDir+pkgsdir)
                for pdir in pkgdir:
                    pkgcat = pkgsdir.split("/")[len(pkgsdir.split("/"))-1]
                    pkgatom = pkgcat+"/"+pdir
                    if pkgatom.find("-MERGING-") == -1:
                        installedAtoms.add(pkgatom)
        return list(installedAtoms), len(installedAtoms)

    def get_installed_packages_counter(self, dbdir = None):
        if not dbdir:
            appDbDir = self.get_vdb_path()
        else:
            appDbDir = dbdir
        installedAtoms = set()

        for current_dirpath, subdirs, files in os.walk(appDbDir):
            pvs = os.listdir(current_dirpath)
            for mypv in pvs:
                if mypv.startswith("-MERGING-"):
                    continue
                mypvpath = current_dirpath+"/"+mypv
                if not os.path.isdir(mypvpath):
                    continue
                mycounter_file = mypvpath+"/"+etpConst['spm']['xpak_entries']['counter']
                if not os.access(mycounter_file,os.R_OK):
                    continue
                f = open(mycounter_file)
                try:
                    counter = int(f.readline().strip())
                except (IOError, ValueError):
                    f.close()
                    continue
                installedAtoms.add((os.path.basename(current_dirpath)+"/"+mypv,counter))
        return installedAtoms

    def refill_counter(self, dbdir = None):
        if not dbdir:
            appDbDir = self.get_vdb_path()
        else:
            appDbDir = dbdir
        counters = set()
        for catdir in os.listdir(appDbDir):
            catdir = appDbDir+catdir
            if not os.path.isdir(catdir):
                continue
            for pkgdir in os.listdir(catdir):
                pkgdir = catdir+"/"+pkgdir
                if not os.path.isdir(pkgdir):
                    continue
                counterfile = pkgdir+"/"+etpConst['spm']['xpak_entries']['counter']
                if not os.path.isfile(pkgdir+"/"+etpConst['spm']['xpak_entries']['counter']):
                    continue
                try:
                    f = open(counterfile,"r")
                    counter = int(f.readline().strip())
                    counters.add(counter)
                    f.close()
                except:
                    continue
        if counters:
            newcounter = max(counters)
        else:
            newcounter = 0
        if not os.path.isdir(os.path.dirname(etpConst['edbcounter'])):
            os.makedirs(os.path.dirname(etpConst['edbcounter']))
        try:
            f = open(etpConst['edbcounter'],"w")
        except IOError, e:
            if e[0] == 21:
                shutil.rmtree(etpConst['edbcounter'],True)
                try:
                    os.rmdir(etpConst['edbcounter'])
                except:
                    pass
            f = open(etpConst['edbcounter'],"w")
        f.write(str(newcounter))
        f.flush()
        f.close()
        del counters
        return newcounter


    def spm_doebuild(self, myebuild, mydo, tree, cpv, portage_tmpdir = None, licenses = []):

        rc = self.entropyTools.spawnFunction(
            self._portage_doebuild,
            myebuild,
            mydo,
            tree,
            cpv,
            portage_tmpdir,
            licenses
        )
        return rc

    def _portage_doebuild(self, myebuild, mydo, tree, cpv, portage_tmpdir = None, licenses = []):
        # myebuild = path/to/ebuild.ebuild with a valid unpacked xpak metadata
        # tree = "bintree"
        # tree = "bintree"
        # cpv = atom
        '''
            # This is a demonstration that Sabayon team love Gentoo so much
            [01:46] <zmedico> if you want something to stay in mysettings
            [01:46] <zmedico> do mysettings.backup_changes("CFLAGS") for example
            [01:46] <zmedico> otherwise your change can get lost inside doebuild()
            [01:47] <zmedico> because it calls mysettings.reset()
            # ^^^ this is DA MAN!
        '''
        # mydbapi = portage.fakedbapi(settings=portage.settings)
        # vartree = portage.vartree(root=myroot)

        oldsystderr = sys.stderr
        f = open("/dev/null","w")
        sys.stderr = f

        ### SETUP ENVIRONMENT
        # if mute, supress portage output
        domute = False
        if etpUi['mute']:
            domute = True
            oldsysstdout = sys.stdout
            sys.stdout = f

        mypath = etpConst['systemroot']+"/"
        os.environ["SKIP_EQUO_SYNC"] = "1"
        os.environ["CD_ROOT"] = "/tmp" # workaround for scripts asking for user intervention
        os.environ["ROOT"] = mypath

        if licenses:
            os.environ["ACCEPT_LICENSE"] = str(' '.join(licenses)) # we already do this early

        # load metadata
        myebuilddir = os.path.dirname(myebuild)
        keys = self.portage.auxdbkeys
        metadata = {}

        for key in keys:
            mykeypath = os.path.join(myebuilddir,key)
            if os.path.isfile(mykeypath) and os.access(mykeypath,os.R_OK):
                f = open(mykeypath,"r")
                metadata[key] = f.readline().strip()
                f.close()

        ### END SETUP ENVIRONMENT

        # find config
        mysettings = self._get_portage_config("/",mypath)
        mysettings['EBUILD_PHASE'] = mydo

        try: # this is a >portage-2.1.4_rc11 feature
            mysettings._environ_whitelist = set(mysettings._environ_whitelist)
            # put our vars into whitelist
            mysettings._environ_whitelist.add("SKIP_EQUO_SYNC")
            mysettings._environ_whitelist.add("ACCEPT_LICENSE")
            mysettings._environ_whitelist.add("CD_ROOT")
            mysettings._environ_whitelist.add("ROOT")
            mysettings._environ_whitelist = frozenset(mysettings._environ_whitelist)
        except:
            pass

        cpv = str(cpv)
        mysettings.setcpv(cpv)
        portage_tmpdir_created = False # for pkg_postrm, pkg_prerm
        if portage_tmpdir:
            if not os.path.isdir(portage_tmpdir):
                os.makedirs(portage_tmpdir)
                portage_tmpdir_created = True
            mysettings['PORTAGE_TMPDIR'] = str(portage_tmpdir)
            mysettings.backup_changes("PORTAGE_TMPDIR")

        mydbapi = self.portage.fakedbapi(settings=mysettings)
        mydbapi.cpv_inject(cpv, metadata = metadata)

        # cached vartree class
        vartree = self._get_portage_vartree(mypath)

        rc = self.portage.doebuild(myebuild = str(myebuild), mydo = str(mydo), myroot = mypath, tree = tree, mysettings = mysettings, mydbapi = mydbapi, vartree = vartree, use_cache = 0)

        # if mute, restore old stdout/stderr
        if domute:
            sys.stdout = oldsysstdout

        sys.stderr = oldsystderr
        f.close()

        if portage_tmpdir_created:
            shutil.rmtree(portage_tmpdir,True)

        del mydbapi
        del metadata
        del keys
        return rc

class LogFile:
    def __init__ (self, level = 0, filename = None, header = "[LOG]"):
        self.handler = self.default_handler
        self.level = level
        self.header = header
        self.logFile = None
        self.open(filename)

    def close (self):
        try:
            self.logFile.close ()
        except:
            pass

    def flush(self):
        self.logFile.flush()

    def fileno(self):
        return self.getFile()

    def isatty(self):
        return False

    def read(self, a):
        return ''

    def readline(self):
        return ''

    def readlines(self):
        return []

    def seek(self, a):
        return self.logFile.seek(a)

    def tell(self):
        return self.logFile.tell()

    def truncate(self):
        return self.logFile.truncate()

    def open (self, file = None):
        if type(file) == type("hello"):
            try:
                self.logFile = open(file, "aw")
            except:
                self.logFile = open("/dev/null", "aw")
        elif file:
            self.logFile = file
        else:
            self.logFile = sys.stderr

    def getFile (self):
        return self.logFile.fileno()

    def __call__(self, format, *args):
        self.handler (format % args)

    def default_handler (self, string):
        self.logFile.write ("* %s\n" % (string))
        self.logFile.flush ()

    def set_loglevel(self, level):
        self.level = level

    def log(self, messagetype, level, message):
        if self.level >= level and not etpUi['nolog']:
            self.handler(self.getTimeDateHeader()+messagetype+' '+self.header+' '+message)

    def write(self, s):
        self.handler(s)

    def writelines(self, lst):
        for s in lst:
            self.write(s)

    def getTimeDateHeader(self):
        return time.strftime('[%X %x %Z] ')

    def ladd(self, level, file, message):
        if self.level >= level:
            self.handler("++ %s \t%s" % (file, message))

    def ldel(self, level, file, message):
        if self.level >= level:
            self.handler("-- %s \t%s" % (file, message))

    def lch(self, level, file, message):
        if self.level >= level:
            self.handler("-+ %s \t%s" % (file, message))

class SocketHostInterface:

    import socket
    import SocketServer
    import entropyTools
    from threading import Thread

    class BasicPamAuthenticator:

        import entropyTools

        def __init__(self):
            self.valid_auth_types = [ "plain", "shadow", "md5" ]

        def docmd_login(self, arguments):

            # filter n00bs
            if not arguments or (len(arguments) != 3):
                return False,None,None,'wrong arguments'

            user = arguments[0]
            auth_type = arguments[1]
            auth_string = arguments[2]

            # check auth type validity
            if auth_type not in self.valid_auth_types:
                return False,user,None,'invalid auth type'

            import pwd
            # check user validty
            try:
                udata = pwd.getpwnam(user)
            except KeyError:
                return False,user,None,'invalid user'

            uid = udata[2]
            # check if user is in the Entropy group
            if not self.entropyTools.is_user_in_entropy_group(uid):
                return False,user,uid,'user not in %s group' % (etpConst['sysgroup'],)

            # now validate password
            valid = self.__validate_auth(user,auth_type,auth_string)
            if not valid:
                return False,user,uid,'auth failed'

            return True,user,uid,"ok"

        def __validate_auth(self, user, auth_type, auth_string):
            valid = False
            if auth_type == "plain":
                valid = self.__do_auth(user, auth_string)
            elif auth_type == "shadow":
                valid = self.__do_auth(user, auth_string, auth_type = "shadow")
            elif auth_type == "md5":
                valid = self.__do_auth(user, auth_string, auth_type = "md5")
            return valid

        def __do_auth(self, user, password, auth_type = None):
            import spwd

            try:
                enc_pass = spwd.getspnam(user)[1]
            except KeyError:
                return False

            if auth_type == None: # plain
                import crypt
                generated_pass = crypt.crypt(str(password), enc_pass)
            elif auth_type == "shadow":
                generated_pass = password
            elif auth_type == "md5": # md5
                import hashlib
                m = hashlib.md5()
                m.update(enc_pass)
                enc_pass = m.hexdigest()
                generated_pass = str(password)
            else: # haha, fuck!
                generated_pass = None

            if generated_pass == enc_pass:
                return True
            return False

        def docmd_logout(self, user):

            # filter n00bs
            if not user or (type(user) is not basestring):
                return False,None,None,"wrong user"

            return True,user,"ok"

        def set_exc_permissions(self, uid, gid):
            if gid != None:
                os.setgid(gid)
            if uid != None:
                os.setuid(uid)

        def hide_login_data(self, args):
            myargs = args[:]
            myargs[-1] = 'hidden'
            return myargs

    class HostServer(SocketServer.ThreadingMixIn, SocketServer.TCPServer):

        class ConnWrapper:
            '''
            Base class for implementing the rest of the wrappers in this module.
            Operates by taking a connection argument which is used when 'self' doesn't
            provide the functionality being requested.
            '''
            def __init__(self, connection) :
                self.connection = connection

            def __getattr__(self, function) :
                return getattr(self.connection, function)

        import socket
        import SocketServer
        # This means the main server will not do the equivalent of a
        # pthread_join() on the new threads.  With this set, Ctrl-C will
        # kill the server reliably.
        daemon_threads = True

        # By setting this we allow the server to re-bind to the address by
        # setting SO_REUSEADDR, meaning you don't have to wait for
        # timeouts when you kill the server and the sockets don't get
        # closed down correctly.
        allow_reuse_address = True

        def __init__(self, server_address, RequestHandlerClass, processor, HostInterface):

            self.processor = processor
            self.server_address = server_address
            self.HostInterface = HostInterface
            self.SSL = self.HostInterface.SSL
            self.real_sock = None

            if self.SSL:
                self.SocketServer.BaseServer.__init__(self, server_address, RequestHandlerClass)
                self.load_ssl_context()
                self.make_ssl_connection_alive()
            else:
                try:
                    self.SocketServer.TCPServer.__init__(self, server_address, RequestHandlerClass)
                except self.socket.error, e:
                    if e[0] == 13:
                        raise exceptionTools.ConnectionError('ConnectionError: %s' % (_("Cannot bind the service"),))
                    raise

        def load_ssl_context(self):
            # setup an SSL context.
            self.context = self.SSL['m'].Context(self.SSL['m'].SSLv23_METHOD)
            self.context.set_verify(self.SSL['m'].VERIFY_PEER, self.verify_ssl_cb)
            # load up certificate stuff.
            self.context.use_privatekey_file(self.SSL['key'])
            self.context.use_certificate_file(self.SSL['cert'])
            self.HostInterface.updateProgress('SSL context loaded, key: %s - cert: %s' % (
                                        self.SSL['key'],
                                        self.SSL['cert'],
                                ))

        def make_ssl_connection_alive(self):
            self.real_sock = self.socket.socket(self.address_family, self.socket_type)
            self.socket = self.ConnWrapper(self.SSL['m'].Connection(self.context, self.real_sock))

            self.server_bind()
            self.server_activate()

        # this function should do the authentication checking to see that
        # the client is who they say they are.
        def verify_ssl_cb(self, conn, cert, errnum, depth, ok) :
            return ok

    class RequestHandler(SocketServer.BaseRequestHandler):

        import SocketServer
        import select
        import socket
        timed_out = False

        def __init__(self, request, client_address, server):
            self.SocketServer.BaseRequestHandler.__init__(self, request, client_address, server)

        def handle(self):

            self.default_timeout = self.server.processor.HostInterface.timeout
            ssl = self.server.processor.HostInterface.SSL
            ssl_exceptions = self.server.processor.HostInterface.SSL_exceptions
            myeos = self.server.processor.HostInterface.answers['eos']

            if self.valid_connection:

                while 1:

                    mylen = -1

                    if self.timed_out:
                        break
                    self.timed_out = True
                    ready_to_read, ready_to_write, in_error = self.select.select([self.request], [], [], self.default_timeout)

                    if len(ready_to_read) == 1 and ready_to_read[0] == self.request:

                        self.timed_out = False

                        try:
                            data = self.request.recv(256)
                            if mylen == -1:
                                if len(data) < len(myeos):
                                    self.server.processor.HostInterface.updateProgress(
                                        'interrupted: %s, reason: %s - from client: %s' % (
                                            self.server.server_address,
                                            "malformed EOS",
                                            self.client_address,
                                        )
                                    )
                                    break
                                mylen = data.split(myeos)[0]
                                data = data[len(mylen)+1:]
                                mylen = int(mylen)
                                mylen -= len(data)
                            while mylen > 0:
                                data += self.request.recv(128)
                                mylen -= 128
                        except ValueError:
                            self.server.processor.HostInterface.updateProgress(
                                'interrupted: %s, reason: %s - from client: %s' % (
                                    self.server.server_address,
                                    "malformed transmission",
                                    self.client_address,
                                )
                            )
                            break
                        except self.socket.timeout, e:
                            self.server.processor.HostInterface.updateProgress(
                                'interrupted: %s, reason: %s - from client: %s' % (
                                    self.server.server_address,
                                    e,
                                    self.client_address,
                                )
                            )
                            break
                        except ssl_exceptions['WantReadError']:
                            continue
                        except ssl_exceptions['Error'], e:
                            self.server.processor.HostInterface.updateProgress(
                                'interrupted: SSL Error, reason: %s - from client: %s' % (
                                    e,
                                    self.client_address,
                                )
                            )
                            break

                        if not data:
                            break

                        cmd = self.server.processor.process(data, self.request, self.client_address)
                        if cmd == 'close':
                            break

            self.request.close()

        def setup(self):

            self.valid_connection = True
            allowed = self.max_connections_check(
                self.server.processor.HostInterface.connections,
                self.server.processor.HostInterface.max_connections
            )
            if allowed:
                self.server.processor.HostInterface.connections += 1
                self.server.processor.HostInterface.updateProgress(
                    '[from: %s] connection established (%s of %s max connections)' % (
                                        self.client_address,
                                        self.server.processor.HostInterface.connections,
                                        self.server.processor.HostInterface.max_connections,
                                )
                )
                return True

            self.server.processor.HostInterface.updateProgress(
                '[from: %s] connection refused (max connections reached: %s)' % (
                    self.client_address,
                    self.server.processor.HostInterface.max_connections,
                )
            )
            return False

        def finish(self):
            self.server.processor.HostInterface.updateProgress(
                '[from: %s] connection closed (%s of %s max connections)' % (
                    self.client_address,
                    self.server.processor.HostInterface.connections,
                    self.server.processor.HostInterface.max_connections,
                )
            )
            if self.valid_connection:
                self.server.processor.HostInterface.connections -= 1

        def max_connections_check(self, current, maximum):
            if current >= maximum:
                self.server.processor.HostInterface.transmit(
                    self.request,
                    self.server.processor.HostInterface.answers['mcr']
                )
                self.valid_connection = False
                return False
            else:
                return True

    class CommandProcessor:

        import entropyTools

        def __init__(self, HostInterface):
            self.HostInterface = HostInterface
            self.Authenticator = self.HostInterface.Authenticator
            self.channel = None
            self.lastoutput = None

        def handle_termination_commands(self, data):
            if data.strip() in self.HostInterface.termination_commands:
                self.HostInterface.updateProgress('close: %s' % (self.client_address,))
                self.transmit(self.HostInterface.answers['cl'])
                return "close"

            if not data.strip():
                return "ignore"

        def handle_command_string(self, string):
            # validate command
            args = string.strip().split()
            session = args[0]
            if (session in self.HostInterface.initialization_commands) or len(args) < 2:
                cmd = args[0]
                session = None
            else:
                cmd = args[1]
                args = args[1:] # remove session

            myargs = []
            if len(args) > 1:
                myargs = args[1:]

            return cmd,myargs,session

        def handle_end_answer(self, cmd, whoops, valid_cmd):
            if not valid_cmd:
                self.transmit(self.HostInterface.answers['no'])
            elif whoops:
                self.transmit(self.HostInterface.answers['er'])
            elif cmd not in self.HostInterface.no_acked_commands:
                self.transmit(self.HostInterface.answers['ok'])

        def validate_command(self, cmd, args, session):

            # answer to invalid commands
            if (cmd not in self.HostInterface.valid_commands):
                return False,"not a valid command"

            if session == None:
                if cmd not in self.HostInterface.no_session_commands:
                    return False,"need a valid session"
            elif session not in self.HostInterface.sessions:
                return False,"session is not alive"

            # check if command needs authentication
            if session != None:
                auth = self.HostInterface.valid_commands[cmd]['auth']
                if auth:
                    # are we?
                    authed = self.HostInterface.sessions[session]['auth_uid']
                    if authed == None:
                        # nope
                        return False,"not authenticated"

            # keep session alive
            if session != None:
                self.HostInterface.set_session_running(session)
                self.HostInterface.update_session_time(session)

            return True,"all good"

        def load_service_interface(self, session):

            uid = None
            if session != None:
                uid = self.HostInterface.sessions[session]['auth_uid']

            intf = self.HostInterface.EntropyInstantiation[0]
            args = self.HostInterface.EntropyInstantiation[1]
            kwds = self.HostInterface.EntropyInstantiation[2]
            Entropy = intf(*args, **kwds)
            Entropy.urlFetcher = SocketUrlFetcher
            Entropy.updateProgress = self.remoteUpdateProgress
            try:
                Entropy.clientDbconn.updateProgress = self.remoteUpdateProgress
            except AttributeError:
                pass
            Entropy.progress = self.remoteUpdateProgress
            return Entropy

        def process(self, data, channel, client_address):

            self.channel = channel
            self.client_address = client_address

            if data.strip():
                mycommand = data.strip().split()
                if mycommand[0] in self.HostInterface.login_pass_commands:
                    mycommand = self.Authenticator.hide_login_data(mycommand)
                self.HostInterface.updateProgress("[from: %s] call: %s" % (
                                self.client_address,
                                repr(' '.join(mycommand)),
                            )
                )

            term = self.handle_termination_commands(data)
            if term:
                return term

            cmd, args, session = self.handle_command_string(data)
            valid_cmd, reason = self.validate_command(cmd, args, session)

            p_args = args
            if cmd in self.HostInterface.login_pass_commands:
                p_args = self.Authenticator.hide_login_data(p_args)
            self.HostInterface.updateProgress(
                '[from: %s] command validation :: called %s: args: %s, session: %s, valid: %s, reason: %s' % (
                    self.client_address,
                    cmd,
                    p_args,
                    session,
                    valid_cmd,
                    reason,
                )
            )

            whoops = False
            if valid_cmd:
                Entropy = self.load_service_interface(session)
                try:
                    self.run_task(cmd, args, session, Entropy)
                except Exception, e:
                    self.entropyTools.printTraceback()
                    # store error
                    self.HostInterface.updateProgress(
                        '[from: %s] command error: %s, type: %s' % (
                            self.client_address,
                            e,
                            type(e),
                        )
                    )
                    if session != None:
                        self.HostInterface.store_rc(str(e),session)
                    whoops = True

            if session != None:
                self.HostInterface.update_session_time(session)
                self.HostInterface.unset_session_running(session)
            self.handle_end_answer(cmd, whoops, valid_cmd)

        def transmit(self, data):
            self.HostInterface.transmit(self.channel, data)

        def remoteUpdateProgress(
                self,
                text,
                header = "",
                footer = "",
                back = False,
                importance = 0,
                type = "info",
                count = [],
                percent = False
            ):
                if text != self.lastoutput:
                    text = chr(27)+"[2K\r"+text
                    if not back:
                        text += "\n"
                    self.transmit(text)
                self.lastoutput = text

        def run_task(self, cmd, args, session, Entropy):

            p_args = args
            if cmd in self.HostInterface.login_pass_commands:
                p_args = self.Authenticator.hide_login_data(p_args)
            self.HostInterface.updateProgress(
                '[from: %s] run_task :: called %s: args: %s, session: %s' % (
                    self.client_address,
                    cmd,
                    p_args,
                    session,
                )
            )

            myargs, mykwargs = self._get_args_kwargs(args)

            rc = self.spawn_function(cmd, myargs, mykwargs, session, Entropy)
            if session != None and self.HostInterface.sessions.has_key(session):
                self.HostInterface.store_rc(rc, session)
            return rc

        def _get_args_kwargs(self, args):
            myargs = []
            mykwargs = {}
            for arg in args:
                if (arg.find("=") != -1) and not arg.startswith("="):
                    x = arg.split("=")
                    a = x[0]
                    b = ''.join(x[1:])
                    mykwargs[a] = eval(b)
                else:
                    try:
                        myargs.append(eval(arg))
                    except (NameError, SyntaxError):
                        myargs.append(str(arg))
            return myargs, mykwargs

        def spawn_function(self, cmd, myargs, mykwargs, session, Entropy):

            p_args = myargs
            if cmd in self.HostInterface.login_pass_commands:
                p_args = self.Authenticator.hide_login_data(p_args)
            self.HostInterface.updateProgress(
                '[from: %s] called %s: args: %s, kwargs: %s' % (
                    self.client_address,
                    cmd,
                    p_args,
                    mykwargs,
                )
            )
            return self.do_spawn(cmd, myargs, mykwargs, session, Entropy)

        def do_spawn(self, cmd, myargs, mykwargs, session, Entropy):

            cmd_data = self.HostInterface.valid_commands.get(cmd)
            do_fork = cmd_data['as_user']
            f = cmd_data['cb']
            func_args = []
            for arg in cmd_data['args']:
                try:
                    func_args.append(eval(arg))
                except (NameError, SyntaxError):
                    func_args.append(str(arg))

            if do_fork:
                myfargs = func_args[:]
                myfargs.extend(myargs)
                return self.fork_task(f, session, *myfargs, **mykwargs)
            else:
                return f(*func_args)

        def fork_task(self, f, session, *args, **kwargs):
            gid = None
            uid = None
            if session != None:
                logged_in = self.HostInterface.sessions[session]['auth_uid']
                if logged_in != None:
                    uid = logged_in
                    gid = etpConst['entropygid']
            return self.entropyTools.spawnFunction(self._do_fork, f, uid, gid, *args, **kwargs)

        def _do_fork(self, f, uid, gid, *args, **kwargs):
            self.Authenticator.set_exc_permissions(uid,gid)
            rc = f(*args,**kwargs)
            return rc

    class BuiltInCommands:

        import dumpTools

        def __str__(self):
            return self.inst_name

        def __init__(self, HostInterface, Authenticator):

            self.HostInterface = HostInterface
            self.Authenticator = Authenticator
            self.inst_name = "builtin"

            self.valid_commands = {
                'begin':    {
                                'auth': False, # does it need authentication ?
                                'built_in': True, # is it built-in ?
                                'cb': self.docmd_begin, # function to call
                                'args': ["self.transmit"], # arguments to be passed before *args and **kwards
                                'as_user': False, # do I have to fork the process and run it as logged user?
                                                  # needs auth = True
                                'desc': "instantiate a session", # description
                                'syntax': "begin", # syntax
                                'from': str(self), # from what class
                            },
                'end':      {
                                'auth': False,
                                'built_in': True,
                                'cb': self.docmd_end,
                                'args': ["self.transmit", "session"],
                                'as_user': False,
                                'desc': "end a session",
                                'syntax': "<SESSION_ID> end",
                                'from': str(self),
                            },
                'session_config':      {
                                'auth': False,
                                'built_in': True,
                                'cb': self.docmd_session_config,
                                'args': ["session","myargs"],
                                'as_user': False,
                                'desc': "set session configuration options",
                                'syntax': "<SESSION_ID> session_config <option> [parameters]",
                                'from': str(self),
                            },
                'reposync': {
                                'auth': True,
                                'built_in': True,
                                'cb': self.docmd_reposync,
                                'args': ["Entropy"],
                                'as_user': True,
                                'desc': "update repositories",
                                'syntax': "<SESSION_ID> reposync (optionals: reponames=['repoid1'] forceUpdate=Bool "
                                          "noEquoCheck=Bool fetchSecurity=Bool",
                                'from': str(self),
                            },
                'rc':       {
                                'auth': False,
                                'built_in': True,
                                'cb': self.docmd_rc,
                                'args': ["self.transmit","session"],
                                'as_user': False,
                                'desc': "get data returned by the last valid command (streamed python object)",
                                'syntax': "<SESSION_ID> rc",
                                'from': str(self),
                            },
                'match':    {
                                'auth': False,
                                'built_in': True,
                                'cb': self.docmd_match,
                                'args': ["Entropy"],
                                'as_user': True,
                                'desc': "match an atom inside configured repositories",
                                'syntax': "<SESSION_ID> match app-foo/foo",
                                'from': str(self),
                            },
                'hello':    {
                                'auth': False,
                                'built_in': True,
                                'cb': self.docmd_hello,
                                'args': ["self.transmit"],
                                'as_user': False,
                                'desc': "get server status",
                                'syntax': "hello",
                                'from': str(self),
                            },
                'alive':    {
                                'auth': True,
                                'built_in': True,
                                'cb': self.docmd_alive,
                                'args': ["self.transmit","session"],
                                'as_user': False,
                                'desc': "check if a session is still alive",
                                'syntax': "<SESSION_ID> alive",
                                'from': str(self),
                            },
                'login':    {
                                'auth': False,
                                'built_in': True,
                                'cb': self.docmd_login,
                                'args': ["self.transmit", "session", "self.client_address", "myargs"],
                                'as_user': False,
                                'desc': "login on the running server (allows running extra commands)",
                                'syntax': "<SESSION_ID> login <USER> <AUTH_TYPE: plain,shadow,md5> <PASSWORD>",
                                'from': str(self),
                            },
                'logout':   {
                                'auth': True,
                                'built_in': True,
                                'cb': self.docmd_logout,
                                'args': ["self.transmit","session", "myargs"],
                                'as_user': False,
                                'desc': "logout on the running server",
                                'syntax': "<SESSION_ID> logout <USER>",
                                'from': str(self),
                            },
                'help':   {
                                'auth': False,
                                'built_in': True,
                                'cb': self.docmd_help,
                                'args': ["self.transmit"],
                                'as_user': False,
                                'desc': "this output",
                                'syntax': "help",
                                'from': str(self),
                            },

            }

            self.no_acked_commands = ["rc", "begin", "end", "hello", "alive", "login", "logout","help"]
            self.termination_commands = ["quit","close"]
            self.initialization_commands = ["begin"]
            self.login_pass_commands = ["login"]
            self.no_session_commands = ["begin","hello","alive","help"]

        def register(
                self,
                valid_commands,
                no_acked_commands,
                termination_commands,
                initialization_commands,
                login_pass_commads,
                no_session_commands
            ):
            valid_commands.update(self.valid_commands)
            no_acked_commands.extend(self.no_acked_commands)
            termination_commands.extend(self.termination_commands)
            initialization_commands.extend(self.initialization_commands)
            login_pass_commads.extend(self.login_pass_commands)
            no_session_commands.extend(self.no_session_commands)

        def docmd_session_config(self, session, myargs):

            if not myargs:
                return False,"not enough parameters"

            option = myargs[0]
            myopts = myargs[1:]

            if option == "compression":
                docomp = True
                if myopts:
                    if isinstance(myopts[0],bool):
                        docomp = myopts[0]
                    else:
                        try:
                            docomp = eval(myopts[0])
                        except (NameError, TypeError,):
                            pass
                self.HostInterface.sessions[session]['compression'] = docomp
                return True,"compression now: %s" % (docomp,)
            else:
                return False,"invalid config option"


        def docmd_login(self, transmitter, session, client_address, myargs):

            # is already auth'd?
            auth_uid = self.HostInterface.sessions[session]['auth_uid']
            if auth_uid != None:
                return False,"already authenticated"

            status, user, uid, reason = self.Authenticator.docmd_login(myargs)
            if status:
                self.HostInterface.updateProgress(
                    '[from: %s] user %s logged in successfully, session: %s' % (
                        client_address,
                        user,
                        session,
                    )
                )
                self.HostInterface.sessions[session]['auth_uid'] = uid
                transmitter(self.HostInterface.answers['ok'])
                return True,reason
            elif user == None:
                self.HostInterface.updateProgress(
                    '[from: %s] user -not specified- login failed, session: %s, reason: %s' % (
                        client_address,
                        session,
                        reason,
                    )
                )
                transmitter(self.HostInterface.answers['no'])
                return False,reason
            else:
                self.HostInterface.updateProgress(
                    '[from: %s] user %s login failed, session: %s, reason: %s' % (
                        client_address,
                        user,
                        session,
                        reason,
                    )
                )
                transmitter(self.HostInterface.answers['no'])
                return False,reason

        def docmd_logout(self, transmitter, session, myargs):
            status, user, reason = self.Authenticator.docmd_logout(myargs)
            if status:
                self.HostInterface.updateProgress(
                    '[from: %s] user %s logged out successfully, session: %s, args: %s ' % (
                        self.client_address,
                        user,
                        session,
                        myargs,
                    )
                )
                self.HostInterface.sessions[session]['auth_uid'] = None
                transmitter(self.HostInterface.answers['ok'])
                return True,reason
            elif user == None:
                self.HostInterface.updateProgress(
                    '[from: %s] user -not specified- logout failed, session: %s, args: %s, reason: %s' % (
                        self.client_address,
                        session,
                        myargs,
                        reason,
                    )
                )
                transmitter(self.HostInterface.answers['no'])
                return False,reason
            else:
                self.HostInterface.updateProgress(
                    '[from: %s] user %s logout failed, session: %s, args: %s, reason: %s' % (
                        self.client_address,
                        user,
                        session,
                        myargs,
                        reason,
                    )
                )
                transmitter(self.HostInterface.answers['no'])
                return False,reason

        def docmd_alive(self, transmitter, session):
            cmd = self.HostInterface.answers['no']
            if session in self.HostInterface.sessions:
                cmd = self.HostInterface.answers['ok']
            transmitter(cmd)

        def docmd_hello(self, transmitter):
            uname = os.uname()
            kern_string = uname[2]
            running_host = uname[1]
            running_arch = uname[4]
            load_stats = commands.getoutput('uptime').split("\n")[0]
            text = "Entropy Server %s, connections: %s ~ running on: %s ~ host: %s ~ arch: %s, kernel: %s, stats: %s\n" % (
                    etpConst['entropyversion'],
                    self.HostInterface.connections,
                    etpConst['systemname'],
                    running_host,
                    running_arch,
                    kern_string,
                    load_stats
                    )
            transmitter(text)

        def docmd_help(self, transmitter):
            text = '\nEntropy Socket Interface Help Menu\n' + \
                   'Available Commands:\n\n'
            valid_cmds = self.HostInterface.valid_commands.keys()
            valid_cmds.sort()
            for cmd in valid_cmds:
                if self.HostInterface.valid_commands[cmd].has_key('desc'):
                    desc = self.HostInterface.valid_commands[cmd]['desc']
                else:
                    desc = 'no description available'

                if self.HostInterface.valid_commands[cmd].has_key('syntax'):
                    syntax = self.HostInterface.valid_commands[cmd]['syntax']
                else:
                    syntax = 'no syntax available'
                if self.HostInterface.valid_commands[cmd].has_key('from'):
                    myfrom = self.HostInterface.valid_commands[cmd]['from']
                else:
                    myfrom = 'N/A'
                text += "[%s] %s\n   %s: %s\n   %s: %s\n" % (
                    myfrom,
                    blue(cmd),
                    red("description"),
                    desc.strip(),
                    darkgreen("syntax"),
                    syntax,
                )
            transmitter(text)

        def docmd_end(self, transmitter, session):
            rc = self.HostInterface.destroy_session(session)
            cmd = self.HostInterface.answers['no']
            if rc: cmd = self.HostInterface.answers['ok']
            transmitter(cmd)
            return rc

        def docmd_begin(self, transmitter):
            session = self.HostInterface.get_new_session()
            transmitter(session)
            return session

        def docmd_rc(self, transmitter, session):
            rc = self.HostInterface.get_rc(session)
            comp = self.HostInterface.sessions[session]['compression']
            if comp:
                import gzip
                try:
                    import cStringIO as stringio
                except ImportError:
                    import StringIO as stringio
                f = stringio.StringIO()

                self.dumpTools.serialize(rc, f)
                myf = stringio.StringIO()
                mygz = gzip.GzipFile(
                    mode = 'wb',
                    fileobj = myf
                )
                f.seek(0)
                chunk = f.read(8192)
                while chunk:
                    mygz.write(chunk)
                    chunk = f.read(8192)
                mygz.flush()
                mygz.close()
                transmitter(myf.getvalue())
                f.close()
                myf.close()
            else:
                try:
                    import cStringIO as stringio
                except ImportError:
                    import StringIO as stringio
                f = stringio.StringIO()
                self.dumpTools.serialize(rc, f)
                transmitter(f.getvalue())
                f.close()

            return rc

        def docmd_match(self, Entropy, *myargs, **mykwargs):
            return Entropy.atomMatch(*myargs, **mykwargs)

        def docmd_reposync(self, Entropy, *myargs, **mykwargs):
            repoConn = Entropy.Repositories(*myargs, **mykwargs)
            return repoConn.sync()

    def __init__(self, service_interface, *args, **kwds):

        self.args = args
        self.kwds = kwds
        self.socketLog = LogFile(level = 2, filename = etpConst['socketlogfile'], header = "[Socket]")

        # settings
        self.timeout = etpConst['socket_service']['timeout']
        self.hostname = etpConst['socket_service']['hostname']
        self.session_ttl = etpConst['socket_service']['session_ttl']
        if self.hostname == "*": self.hostname = ''
        self.port = etpConst['socket_service']['port']
        self.threads = etpConst['socket_service']['threads'] # maximum number of allowed sessions
        self.max_connections = etpConst['socket_service']['max_connections']
        self.disabled_commands = etpConst['socket_service']['disabled_cmds']
        self.connections = 0
        self.sessions = {}
        self.answers = etpConst['socket_service']['answers']
        self.Server = None
        self.Gc = None
        self.__output = None
        self.SSL = {}
        self.SSL_exceptions = {}
        self.SSL_exceptions['WantReadError'] = None
        self.SSL_exceptions['Error'] = []
        self.last_print = ''
        self.valid_commands = {}
        self.no_acked_commands = []
        self.termination_commands = []
        self.initialization_commands = []
        self.login_pass_commands = []
        self.no_session_commands = []
        self.command_classes = [self.BuiltInCommands]
        self.command_instances = []
        self.EntropyInstantiation = (service_interface, self.args, self.kwds)

        self.setup_external_command_classes()
        self.start_local_output_interface()
        self.start_authenticator()
        self.setup_hostname()
        self.setup_commands()
        self.disable_commands()
        self.start_session_garbage_collector()
        self.setup_ssl()

    def append_eos(self, data):
        return str(len(data)) + \
            self.answers['eos'] + \
                data

    def setup_ssl(self):

        do_ssl = False
        if self.kwds.has_key('ssl'):
            do_ssl = self.kwds.pop('ssl')

        if not do_ssl:
            return

        try:
            from OpenSSL import SSL
        except ImportError, e:
            self.updateProgress('Unable to load OpenSSL, error: %s' % (repr(e),))
            return
        self.SSL_exceptions['WantReadError'] = SSL.WantReadError
        self.SSL_exceptions['Error'] = SSL.Error
        self.SSL['m'] = SSL
        self.SSL['key'] = etpConst['socket_service']['ssl_key'] # openssl genrsa -out filename.key 1024
        self.SSL['cert'] = etpConst['socket_service']['ssl_cert'] # openssl req -new -days 365 -key filename.key -x509 -out filename.cert
        # change port
        self.port = etpConst['socket_service']['ssl_port']

        if not os.path.isfile(self.SSL['key']):
            raise exceptionTools.FileNotFound('FileNotFound: no %s found' % (self.SSL['key'],))
        if not os.path.isfile(self.SSL['cert']):
            raise exceptionTools.FileNotFound('FileNotFound: no %s found' % (self.SSL['cert'],))
        os.chmod(self.SSL['key'],0600)
        os.chown(self.SSL['key'],0,0)
        os.chmod(self.SSL['cert'],0644)
        os.chown(self.SSL['cert'],0,0)

    def setup_external_command_classes(self):

        if self.kwds.has_key('external_cmd_classes'):
            ext_commands = self.kwds.pop('external_cmd_classes')
            if type(ext_commands) is not list:
                raise exceptionTools.InvalidDataType("InvalidDataType: external_cmd_classes must be a list")
            self.command_classes += ext_commands

    def setup_commands(self):

        identifiers = set()
        for myclass in self.command_classes:
            myinst = myclass(self,self.Authenticator)
            if str(myinst) in identifiers:
                raise exceptionTools.PermissionDenied("PermissionDenied: another command instance is owning this name")
            identifiers.add(str(myinst))
            self.command_instances.append(myinst)
            # now register
            myinst.register(    self.valid_commands,
                                self.no_acked_commands,
                                self.termination_commands,
                                self.initialization_commands,
                                self.login_pass_commands,
                                self.no_session_commands
                            )

    def disable_commands(self):
        for cmd in self.disabled_commands:

            if cmd in self.valid_commands:
                self.valid_commands.pop(cmd)

            if cmd in self.no_acked_commands:
                self.no_acked_commands.remove(cmd)

            if cmd in self.termination_commands:
                self.termination_commands.remove(cmd)

            if cmd in self.initialization_commands:
                self.initialization_commands.remove(cmd)

            if cmd in self.login_pass_commands:
                self.login_pass_commands.remove(cmd)

            if cmd in self.no_session_commands:
                self.no_session_commands.remove(cmd)

    def start_local_output_interface(self):
        if self.kwds.has_key('sock_output'):
            outputIntf = self.kwds.pop('sock_output')
            self.__output = outputIntf

    def start_authenticator(self):

        auth_inst = (self.BasicPamAuthenticator, [], {}) # authentication class, args, keywords
        # external authenticator
        if self.kwds.has_key('sock_auth'):
            authIntf = self.kwds.pop('sock_auth')
            if type(authIntf) is tuple:
                if len(authIntf) == 3:
                    auth_inst = authIntf[:]
                else:
                    raise exceptionTools.IncorrectParameter("IncorrectParameter: wront authentication interface specified")
            else:
                raise exceptionTools.IncorrectParameter("IncorrectParameter: wront authentication interface specified")
            # initialize authenticator
        self.Authenticator = auth_inst[0](*auth_inst[1], **auth_inst[2])

    def start_session_garbage_collector(self):
        self.Gc = self.entropyTools.TimeScheduled( self.gc_clean, 5 )
        self.Gc.setName("Socket_GC::"+str(random.random()))
        self.Gc.start()

    def gc_clean(self):
        if not self.sessions:
            return

        for session_id in self.sessions.keys():
            sess_time = self.sessions[session_id]['t']
            is_running = self.sessions[session_id]['running']
            auth_uid = self.sessions[session_id]['auth_uid'] # is kept alive?
            if (is_running) or (auth_uid == -1):
                if auth_uid == -1:
                    self.updateProgress('not killing session %s, since it is kept alive by auth_uid=-1' % (session_id,) )
                continue
            cur_time = time.time()
            ttl = self.session_ttl
            check_time = sess_time + ttl
            if cur_time > check_time:
                self.updateProgress('killing session %s, ttl: %ss: no activity' % (session_id,ttl,) )
                self.destroy_session(session_id)

    def setup_hostname(self):
        if self.hostname:
            try:
                self.hostname = self.get_ip_address(self.hostname)
            except IOError: # it isn't a device name
                pass

    def get_ip_address(self, ifname):
        import fcntl
        import struct
        mysock = self.socket.socket ( self.socket.AF_INET, self.socket.SOCK_STREAM )
        return self.socket.inet_ntoa(fcntl.ioctl(mysock.fileno(), 0x8915, struct.pack('256s', ifname[:15]))[20:24])

    def get_new_session(self):
        if len(self.sessions) > self.threads:
            # fuck!
            return "0"
        rng = str(int(random.random()*100000000000000000)+1)
        while rng in self.sessions:
            rng = str(int(random.random()*100000000000000000)+1)
        self.sessions[rng] = {}
        self.sessions[rng]['running'] = False
        self.sessions[rng]['auth_uid'] = None
        self.sessions[rng]['compression'] = False
        self.sessions[rng]['t'] = time.time()
        return rng

    def update_session_time(self, session):
        if self.sessions.has_key(session):
            self.sessions[session]['t'] = time.time()
            self.updateProgress('session time updated for %s' % (session,) )

    def set_session_running(self, session):
        if self.sessions.has_key(session):
            self.sessions[session]['running'] = True

    def unset_session_running(self, session):
        if self.sessions.has_key(session):
            self.sessions[session]['running'] = False

    def destroy_session(self, session):
        if self.sessions.has_key(session):
            del self.sessions[session]
            return True
        return False

    def go(self):
        self.socket.setdefaulttimeout(self.timeout)
        while 1:
            try:
                self.Server = self.HostServer(
                                                (self.hostname, self.port),
                                                self.RequestHandler,
                                                self.CommandProcessor(self),
                                                self
                                            )
                break
            except self.socket.error, e:
                if e[0] == 98:
                    # Address already in use
                    self.updateProgress('address already in use (%s, port: %s), waiting 5 seconds...' % (self.hostname,self.port,))
                    time.sleep(5)
                    continue
                else:
                    raise
        self.updateProgress('server connected, listening on: %s, port: %s, timeout: %s' % (self.hostname,self.port,self.timeout,))
        self.Server.serve_forever()
        self.Gc.kill()

    def store_rc(self, rc, session):
        if type(rc) in (list,tuple,):
            rc_item = rc[:]
        elif type(rc) in (set,frozenset,dict,):
            rc_item = rc.copy()
        else:
            rc_item = rc
        self.sessions[session]['rc'] = rc_item

    def get_rc(self, session):
        return self.sessions[session]['rc']

    def transmit(self, channel, data):
        channel.sendall(self.append_eos(data))

    def updateProgress(self, *args, **kwargs):
        message = args[0]
        if message != self.last_print:
            self.socketLog.log(ETP_LOGPRI_INFO,ETP_LOGLEVEL_NORMAL,str(args[0]))
            if self.__output != None:
                self.__output.updateProgress(*args,**kwargs)
            self.last_print = message

class SocketUrlFetcher(urlFetcher):

    import entropyTools
    # reimplementing updateProgress
    def updateProgress(self):
        kbprogress = " (%s/%s kB @ %s)" % (
                                        str(round(float(self.downloadedsize)/1024,1)),
                                        str(round(self.remotesize,1)),
                                        str(self.entropyTools.bytesIntoHuman(self.datatransfer))+"/sec",
                                    )
        mytxt = _("Fetch")
        self.progress( mytxt+": "+str((round(float(self.average),1)))+"%"+kbprogress, back = True )


class ServerInterface(TextInterface):

    def __init__(self, default_repository = None, save_repository = False, community_repo = False):

        if etpConst['uid'] != 0:
            mytxt = _("Entropy ServerInterface must be run as root")
            raise exceptionTools.PermissionDenied("PermissionDenied: %s" % (mytxt,))

        self.serverLog = LogFile(
            level = etpConst['entropyloglevel'],
            filename = etpConst['entropylogfile'],
            header = "[server]"
        )

        self.community_repo = community_repo
        self.dbapi2 = dbapi2 # export for third parties
        # settings
        etpSys['serverside'] = True
        self.indexing = False
        self.xcache = False
        self.MirrorsService = None
        self.FtpInterface = FtpInterface
        self.rssFeed = rssFeed
        self.serverDbCache = {}
        self.settings_to_backup = []
        self.do_save_repository = save_repository
        self.default_repository = default_repository
        if self.default_repository == None:
            self.default_repository = etpConst['officialserverrepositoryid']

        if self.default_repository not in etpConst['server_repositories']:
            raise exceptionTools.PermissionDenied("PermissionDenied: %s %s" % (
                        self.default_repository,
                        _("repository not configured"),
                    )
            )
        if etpConst['clientserverrepoid'] in etpConst['server_repositories']:
            raise exceptionTools.PermissionDenied("PermissionDenied: %s %s" % (
                        etpConst['clientserverrepoid'],
                        _("protected repository id, can't use this, sorry dude..."),
                    )
            )

        if self.community_repo:
            self.add_client_database_to_repositories()
        self.switch_default_repository(self.default_repository)

    def __del__(self):
        self.close_server_databases()

    def add_client_database_to_repositories(self):
        etpConst['server_repositories'][etpConst['clientserverrepoid']] = {}
        mydata = {}
        mydata['description'] = "Community Repositories System Database"
        mydata['mirrors'] = []
        mydata['community'] = False
        etpConst['server_repositories'][etpConst['clientserverrepoid']].update(mydata)

    def setup_services(self):
        self.setup_entropy_settings()
        self.ClientService = EquoInterface(
            indexing = self.indexing,
            xcache = self.xcache,
            repo_validation = False,
            noclientdb = 1
        )
        self.ClientService.FtpInterface = self.FtpInterface
        self.validRepositories = self.ClientService.validRepositories
        self.entropyTools = self.ClientService.entropyTools
        self.dumpTools = self.ClientService.dumpTools
        self.QA = self.ClientService.QA
        self.backup_entropy_settings()
        self.SpmService = self.ClientService.Spm()
        self.MirrorsService = ServerMirrorsInterface(self)

    def setup_entropy_settings(self, repo = None):
        backup_list = [
            'etpdatabaseclientfilepath',
            'clientdbid',
            'officialserverrepositoryid'
        ]
        for setting in backup_list:
            if setting not in self.settings_to_backup:
                self.settings_to_backup.append(setting)
        # setup client database
        if not self.community_repo:
            etpConst['etpdatabaseclientfilepath'] = self.get_local_database_file(repo)
            etpConst['clientdbid'] = etpConst['serverdbid']
        const_createWorkingDirectories()

    def close_server_databases(self):
        for item in self.serverDbCache:
            self.serverDbCache[item].closeDB()
        self.serverDbCache.clear()

    def close_server_database(self, dbinstance):
        found = None
        for item in self.serverDbCache:
            if dbinstance == self.serverDbCache[item]:
                found = item
                break
        if found:
            instance = self.serverDbCache.pop(found)
            instance.closeDB()

    def switch_default_repository(self, repoid, save = None):

        # avoid setting __default__ as default server repo
        if repoid == etpConst['clientserverrepoid']:
            return

        if save == None:
            save = self.do_save_repository
        if repoid not in etpConst['server_repositories']:
            raise exceptionTools.PermissionDenied("PermissionDenied: %s %s" % (
                        repoid,
                        _("repository not configured"),
                    )
            )
        self.close_server_databases()
        etpConst['officialserverrepositoryid'] = repoid
        self.default_repository = repoid
        self.setup_services()
        if save:
            self.save_default_repository(repoid)

        self.setup_community_repositories_settings()
        self.show_interface_status()
        self.handle_uninitialized_repository(repoid)

    def setup_community_repositories_settings(self):
        if self.community_repo:
            for repoid in etpConst['server_repositories']:
                etpConst['server_repositories'][repoid]['community'] = True


    def handle_uninitialized_repository(self, repoid):
        if not self.is_repository_initialized(repoid):
            mytxt = blue("%s.") % (_("Your default repository is not initialized"),)
            self.updateProgress(
                "[%s:%s] %s" % (
                        brown("repo"),
                        purple(repoid),
                        mytxt,
                ),
                importance = 1,
                type = "warning",
                header = darkred(" !!! ")
            )
            answer = self.askQuestion(_("Do you want to initialize your default repository ?"))
            if answer == "No":
                mytxt = red("%s.") % (_("You have taken the risk to continue with an uninitialized repository"),)
                self.updateProgress(
                    "[%s:%s] %s" % (
                            brown("repo"),
                            purple(repoid),
                            mytxt,
                    ),
                    importance = 1,
                    type = "warning",
                    header = darkred(" !!! ")
                )
            else:
                # move empty database for security sake
                dbfile = self.get_local_database_file(repoid)
                if os.path.isfile(dbfile):
                    shutil.move(dbfile,dbfile+".backup")
                self.initialize_server_database(empty = True, repo = repoid, warnings = False)


    def show_interface_status(self):
        type_txt = _("server-side repository")
        if self.community_repo:
            type_txt = _("community repository")
        mytxt = _("Entropy Server Interface Instance on repository") # ..on repository: <repository_name>
        self.updateProgress(
            blue("%s: %s (%s: %s)" % (
                    mytxt,
                    red(self.default_repository),
                    purple(_("type")),
                    bold(type_txt),
                )
            ),
            importance = 2,
            type = "info",
            header = red(" @@ ")
        )
        repos = etpConst['server_repositories'].keys()
        mytxt = blue("%s:") % (_("Currently configured repositories"),) # ...: <list>
        self.updateProgress(
                mytxt,
                importance = 1,
                type = "info",
                header = red(" @@ ")
        )
        for repo in repos:
            self.updateProgress(
                darkgreen(repo),
                importance = 0,
                type = "info",
                header = brown("   # ")
            )


    def save_default_repository(self, repoid):

        # avoid setting __default__ as default server repo
        if repoid == etpConst['clientserverrepoid']:
            return

        if os.path.isfile(etpConst['serverconf']):
            f = open(etpConst['serverconf'],"r")
            content = f.readlines()
            f.close()
            content = [x.strip() for x in content]
            found = False
            new_content = []
            for line in content:
                if line.strip().startswith("officialserverrepositoryid|"):
                    line = "officialserverrepositoryid|%s" % (repoid,)
                    found = True
                new_content.append(line)
            if not found:
                new_content.append("officialserverrepositoryid|%s" % (repoid,))
            f = open(etpConst['serverconf']+".save_default_repo_tmp","w")
            for line in new_content:
                f.write(line+"\n")
            f.flush()
            f.close()
            shutil.move(etpConst['serverconf']+".save_default_repo_tmp",etpConst['serverconf'])
        else:
            f = open(etpConst['serverconf'],"w")
            f.write("officialserverrepositoryid|%s\n" % (repoid,))
            f.flush()
            f.close()

    def toggle_repository(self, repoid, enable = True):

        # avoid setting __default__ as default server repo
        if repoid == etpConst['clientserverrepoid']:
            return False

        if not os.path.isfile(etpConst['serverconf']):
            return None
        f = open(etpConst['serverconf'])
        tmpfile = etpConst['serverconf']+".switch"
        mycontent = [x.strip() for x in f.readlines()]
        f.close()
        f = open(tmpfile,"w")
        st = "repository|%s" % (repoid,)
        status = False
        for line in mycontent:
            if enable:
                if (line.find(st) != -1) and line.startswith("#") and (len(line.split("|")) == 5):
                    line = line[1:]
                    status = True
            else:
                if (line.find(st) != -1) and not line.startswith("#") and (len(line.split("|")) == 5):
                    line = "#"+line
                    status = True
            f.write(line+"\n")
        f.flush()
        f.close()
        shutil.move(tmpfile,etpConst['serverconf'])
        if status:
            self.close_server_databases()
            const_readServerSettings()
            self.setup_services()
            self.show_interface_status()
        return status

    def backup_entropy_settings(self):
        for setting in self.settings_to_backup:
            self.ClientService.backup_setting(setting)


    def is_repository_initialized(self, repo):
        dbc = self.openServerDatabase(just_reading = True, repo = repo)
        valid = True
        try:
            dbc.validateDatabase()
        except exceptionTools.SystemDatabaseError:
            valid = False
        self.close_server_database(dbc)
        return valid

    def openServerDatabase(
            self,
            read_only = True,
            no_upload = True,
            just_reading = False,
            repo = None,
            indexing = True,
            warnings = True
        ):

        if repo == None:
            repo = self.default_repository

        if repo == etpConst['clientserverrepoid'] and self.community_repo:
            return self.ClientService.clientDbconn

        if just_reading:
            read_only = True
            no_upload = True

        if etpConst['packagemasking'] == None:
            self.ClientService.parse_masking_settings()

        local_dbfile = self.get_local_database_file(repo)
        cached = self.serverDbCache.get(
                        (   etpConst['systemroot'],
                            local_dbfile,
                            read_only,
                            no_upload,
                            just_reading,
                            repo,
                        )
        )
        if cached != None:
            return cached

        if not os.path.isdir(os.path.dirname(local_dbfile)):
            os.makedirs(os.path.dirname(local_dbfile))

        conn = EntropyDatabaseInterface(
            readOnly = read_only,
            dbFile = local_dbfile,
            noUpload = no_upload,
            OutputInterface = self,
            ServiceInterface = self,
            dbname = etpConst['serverdbid']+repo
        )

        valid = True
        try:
            conn.validateDatabase()
        except exceptionTools.SystemDatabaseError:
            valid = False

        # verify if we need to update the database to sync
        # with portage updates, we just ignore being readonly in the case
        if (repo not in etpConst['server_treeupdatescalled']) and (not just_reading):
            # sometimes, when filling a new server db, we need to avoid tree updates
            if valid:
                conn.serverUpdatePackagesData()
            elif warnings:
                mytxt = _( "Entropy database is probably empty. If you don't agree with what I'm saying, " + \
                           "then it's probably corrupted! I won't stop you here btw..."
                )
                self.updateProgress(
                    darkred(mytxt),
                    importance = 1,
                    type = "warning",
                    header = bold(" !!! ")
                )
        if not read_only and valid and indexing:
            self.updateProgress(
                "[repo:%s|%s] %s" % (
                            blue(repo),
                            red(_("database")),
                            blue(_("indexing database")),
                    ),
                importance = 1,
                type = "info",
                header = brown(" @@ "),
                back = True
            )
            conn.createAllIndexes()

        # !!! also cache just_reading otherwise there will be
        # real issues if the connection is opened several times
        self.serverDbCache[(
                                etpConst['systemroot'],
                                local_dbfile,
                                read_only,
                                no_upload,
                                just_reading,
                                repo,
                            )] = conn
        return conn

    def deps_tester(self):

        server_repos = etpConst['server_repositories'].keys()
        installed_packages = set()
        for repo in server_repos:
            dbconn = self.openServerDatabase(read_only = True, no_upload = True, repo = repo)
            installed_packages |= set([(x,repo) for x in dbconn.listAllIdpackages()])


        deps_not_satisfied = set()
        length = str((len(installed_packages)))
        count = 0
        mytxt = _("Checking")
        for pkgdata in installed_packages:
            count += 1
            idpackage = pkgdata[0]
            repo = pkgdata[1]
            dbconn = self.openServerDatabase(read_only = True, no_upload = True, repo = repo)
            atom = dbconn.retrieveAtom(idpackage)
            self.updateProgress(
                darkgreen(mytxt)+" "+bold(atom),
                importance = 0,
                type = "info",
                back = True,
                count = (count,length),
                header = darkred(" @@  ")
            )

            xdeps = dbconn.retrieveDependencies(idpackage)
            for xdep in xdeps:
                xmatch = self.atomMatch(xdep)
                if xmatch[0] == -1:
                    deps_not_satisfied.add(xdep)

        return deps_not_satisfied

    def dependencies_test(self):

        mytxt = "%s %s" % (blue(_("Running dependencies test")),red("..."))
        self.updateProgress(
            mytxt,
            importance = 2,
            type = "info",
            header = red(" @@ ")
        )

        server_repos = etpConst['server_repositories'].keys()
        deps_not_matched = self.deps_tester()

        if deps_not_matched:

            crying_atoms = {}
            for atom in deps_not_matched:
                for repo in server_repos:
                    dbconn = self.openServerDatabase(just_reading = True, repo = repo)
                    riddep = dbconn.searchDependency(atom)
                    if riddep == -1:
                        continue
                    if riddep != -1:
                        ridpackages = dbconn.searchIdpackageFromIddependency(riddep)
                        for i in ridpackages:
                            iatom = dbconn.retrieveAtom(i)
                            if not crying_atoms.has_key(atom):
                                crying_atoms[atom] = set()
                            crying_atoms[atom].add((iatom,repo))

            mytxt = blue("%s:") % (_("These are the dependencies not found"),)
            self.updateProgress(
                mytxt,
                importance = 1,
                type = "info",
                header = red(" @@ ")
            )
            mytxt = "%s:" % (_("Needed by"),)
            for atom in deps_not_matched:
                self.updateProgress(
                    red(atom),
                    importance = 1,
                    type = "info",
                    header = blue("   # ")
                )
                if crying_atoms.has_key(atom):
                    self.updateProgress(
                        red(mytxt),
                        importance = 0,
                        type = "info",
                        header = blue("      # ")
                    )
                    for x , myrepo in crying_atoms[atom]:
                        self.updateProgress(
                            "[%s:%s] %s" % (blue(_("by repo")),darkred(myrepo),darkgreen(x),),
                            importance = 0,
                            type = "info",
                            header = blue("      # ")
                        )
        else:

            mytxt = blue(_("Every dependency is satisfied. It's all fine."))
            self.updateProgress(
                mytxt,
                importance = 2,
                type = "info",
                header = red(" @@ ")
            )

        return deps_not_matched

    def libraries_test(self, get_files = False, repo = None):

        # load db
        dbconn = self.openServerDatabase(read_only = True, no_upload = True, repo = repo)
        packagesMatched, brokenexecs, status = self.ClientService.libraries_test(dbconn = dbconn)
        if status != 0:
            return 1,None

        if get_files:
            return 0,brokenexecs

        if (not brokenexecs) and (not packagesMatched):
            mytxt = "%s." % (_("System is healthy"),)
            self.updateProgress(
                blue(mytxt),
                importance = 2,
                type = "info",
                header = red(" @@ ")
            )
            return 0,None

        mytxt = "%s:" % (_("Matching libraries with Spm"),)
        self.updateProgress(
            blue(mytxt),
            importance = 1,
            type = "info",
            header = red(" @@ ")
        )

        packages = set()
        mytxt = red("%s: ") % (_("Scanning"),)
        for brokenexec in brokenexecs:
            self.updateProgress(
                mytxt+darkgreen(brokenexec),
                importance = 0,
                type = "info",
                header = red(" @@ "),
                back = True
            )
            packages |= self.SpmService.query_belongs(brokenexec)

        if packages:
            mytxt = "%s:" % (_("These are the matched packages"),)
            self.updateProgress(
                red(mytxt),
                importance = 1,
                type = "info",
                header = red(" @@ ")
            )
            for package in packages:
                self.updateProgress(
                    blue(package),
                    importance = 0,
                    type = "info",
                    header = red("     # ")
                )
        else:
            self.updateProgress(
                red(_("No matched packages")),
                importance = 1,
                type = "info",
                header = red(" @@ ")
            )

        return 0,packages

    def depends_table_initialize(self, repo = None):
        dbconn = self.openServerDatabase(read_only = False, no_upload = True, repo = repo)
        dbconn.regenerateDependsTable()
        dbconn.taintDatabase()
        dbconn.commitChanges()

    def create_empty_database(self, dbpath = None, repo = None):
        if dbpath == None:
            dbpath = self.get_local_database_file(repo)

        dbdir = os.path.dirname(dbpath)
        if not os.path.isdir(dbdir):
            os.makedirs(dbdir)

        mytxt = red("%s ...") % (_("Initializing an empty database file with Entropy structure"),)
        self.updateProgress(
            mytxt,
            importance = 1,
            type = "info",
            header = darkgreen(" * "),
            back = True
        )
        dbconn = self.ClientService.openGenericDatabase(dbpath)
        dbconn.initializeDatabase()
        dbconn.commitChanges()
        dbconn.closeDB()
        mytxt = "%s %s %s." % (red(_("Entropy database file")),bold(dbpath),red(_("successfully initialized")),)
        self.updateProgress(
            mytxt,
            importance = 1,
            type = "info",
            header = darkgreen(" * ")
        )

    def move_packages(self, matches, to_repo, from_repo = None, branch = etpConst['branch'], ask = True):

        switched = set()

        # avoid setting __default__ as default server repo
        if etpConst['clientserverrepoid'] in (to_repo,from_repo):
            self.updateProgress(
                "%s: %s" % (
                        blue(_("You cannot switch packages from/to your system database")),
                        red(etpConst['clientserverrepoid']),
                ),
                importance = 2,
                type = "warning",
                header = darkred(" @@ ")
            )
            return switched

        if not matches and from_repo:
            dbconn = self.openServerDatabase(read_only = True, no_upload = True, repo = from_repo)
            matches = set( \
                [(x,from_repo) for x in \
                    dbconn.listAllIdpackages(branch = branch, branch_operator = "<=")]
            )

        self.updateProgress(
            "%s %s:" % (
                    blue(_("Preparing to move selected packages to")),
                    red(to_repo),
            ),
            importance = 2,
            type = "info",
            header = red(" @@ ")
        )
        self.updateProgress(
            "%s: %s" % (
                    bold(_("Note")),
                    red(_("all the old packages with conflicting scope will" + \
                    " be removed from the destination repo unless injected")),
            ),
            importance = 1,
            type = "info",
            header = red(" @@ ")
        )

        for match in matches:
            repo = match[1]
            dbconn = self.openServerDatabase(read_only = True, no_upload = True, repo = repo)
            self.updateProgress(
                "[%s=>%s|%s] %s" % (
                        darkgreen(repo),
                        darkred(to_repo),
                        brown(branch),
                        blue(dbconn.retrieveAtom(match[0])),
                ),
                importance = 0,
                type = "info",
                header = brown("    # ")
            )


        if ask:
            rc = self.askQuestion(_("Would you like to continue ?"))
            if rc == "No":
                return switched

        for match in matches:
            idpackage = match[0]
            repo = match[1]
            dbconn = self.openServerDatabase(read_only = False, no_upload = True, repo = repo)
            match_branch = dbconn.retrieveBranch(idpackage)
            match_atom = dbconn.retrieveAtom(idpackage)
            package_filename = os.path.basename(dbconn.retrieveDownloadURL(idpackage))
            self.updateProgress(
                "[%s=>%s|%s] %s: %s" % (
                        darkgreen(repo),
                        darkred(to_repo),
                        brown(branch),
                        blue(_("switching")),
                        darkgreen(match_atom),
                ),
                importance = 0,
                type = "info",
                header = red(" @@ "),
                back = True
            )
            # move binary file
            from_file = os.path.join(self.get_local_packages_directory(repo),match_branch,package_filename)
            if not os.path.isfile(from_file):
                from_file = os.path.join(self.get_local_upload_directory(repo),match_branch,package_filename)
            if not os.path.isfile(from_file):
                self.updateProgress(
                    "[%s=>%s|%s] %s: %s -> %s" % (
                            darkgreen(repo),
                            darkred(to_repo),
                            brown(branch),
                            bold(_("cannot switch, package not found, skipping")),
                            darkgreen(),
                            red(from_file),
                    ),
                    importance = 1,
                    type = "warning",
                    header = darkred(" !!! ")
                )
                continue

            to_file = os.path.join(self.get_local_upload_directory(to_repo),match_branch,package_filename)
            if not os.path.isdir(os.path.dirname(to_file)):
                os.makedirs(os.path.dirname(to_file))

            copy_data = [
                            (from_file,to_file,),
                            (from_file+etpConst['packageshashfileext'],to_file+etpConst['packageshashfileext'],),
                            (from_file+etpConst['packagesexpirationfileext'],to_file+etpConst['packagesexpirationfileext'],)
                        ]

            for from_item,to_item in copy_data:
                self.updateProgress(
                        "[%s=>%s|%s] %s: %s" % (
                                darkgreen(repo),
                                darkred(to_repo),
                                brown(branch),
                                blue(_("moving file")),
                                darkgreen(os.path.basename(from_item)),
                        ),
                        importance = 0,
                        type = "info",
                        header = red(" @@ "),
                        back = True
                )
                if os.path.isfile(from_item):
                    shutil.copy2(from_item,to_item)

            self.updateProgress(
                "[%s=>%s|%s] %s: %s" % (
                        darkgreen(repo),
                        darkred(to_repo),
                        brown(branch),
                        blue(_("loading data from source database")),
                        darkgreen(repo),
                ),
                importance = 0,
                type = "info",
                header = red(" @@ "),
                back = True
            )
            # install package into destination db
            data = dbconn.getPackageData(idpackage)
            todbconn = self.openServerDatabase(read_only = False, no_upload = True, repo = to_repo)

            self.updateProgress(
                "[%s=>%s|%s] %s: %s" % (
                        darkgreen(repo),
                        darkred(to_repo),
                        brown(branch),
                        blue(_("injecting data to destination database")),
                        darkgreen(to_repo),
                ),
                importance = 0,
                type = "info",
                header = red(" @@ "),
                back = True
            )
            new_idpackage, new_revision, new_data = todbconn.handlePackage(data)
            del data
            todbconn.commitChanges()

            self.updateProgress(
                "[%s=>%s|%s] %s: %s" % (
                        darkgreen(repo),
                        darkred(to_repo),
                        brown(branch),
                        blue(_("removing entry from source database")),
                        darkgreen(repo),
                ),
                importance = 0,
                type = "info",
                header = red(" @@ "),
                back = True
            )

            # remove package from old db
            dbconn.removePackage(idpackage)
            dbconn.commitChanges()

            self.updateProgress(
                "[%s=>%s|%s] %s: %s" % (
                        darkgreen(repo),
                        darkred(to_repo),
                        brown(branch),
                        blue(_("successfully moved atom")),
                        darkgreen(match_atom),
                ),
                importance = 0,
                type = "info",
                header = blue(" @@ ")
            )
            switched.add(match)

        return switched


    def package_injector(self, package_file, branch = etpConst['branch'], inject = False, repo = None):

        if repo == None:
            repo = self.default_repository

        upload_dir = os.path.join(self.get_local_upload_directory(repo),branch)
        if not os.path.isdir(upload_dir):
            os.makedirs(upload_dir)

        dbconn = self.openServerDatabase(read_only = False, no_upload = True, repo = repo)
        self.updateProgress(
            red("[repo: %s] %s: %s" % (
                        darkgreen(repo),
                        _("adding package"),
                        bold(os.path.basename(package_file)),
                    )
            ),
            importance = 1,
            type = "info",
            header = brown(" * "),
            back = True
        )
        mydata = self.ClientService.extract_pkg_metadata(package_file, etpBranch = branch, inject = inject)
        idpackage, revision, mydata = dbconn.handlePackage(mydata)

        # set trashed counters
        trashing_counters = set()
        myserver_repos = etpConst['server_repositories'].keys()
        for myrepo in myserver_repos:
            mydbconn = self.openServerDatabase(read_only = True, no_upload = True, repo = myrepo)
            mylist = mydbconn.retrieve_packages_to_remove(
                    mydata['name'],
                    mydata['category'],
                    mydata['slot'],
                    branch,
                    mydata['injected']
            )
            for myitem in mylist:
                trashing_counters.add(mydbconn.retrieveCounter(myitem))

        for mycounter in trashing_counters:
            dbconn.setTrashedCounter(mycounter)

        # add package info to our current server repository
        dbconn.removePackageFromInstalledTable(idpackage)
        dbconn.addPackageToInstalledTable(idpackage,repo)
        atom = dbconn.retrieveAtom(idpackage)

        self.updateProgress(
            "[repo:%s] %s: %s %s: %s" % (
                        darkgreen(repo),
                        blue(_("added package")),
                        darkgreen(atom),
                        blue(_("rev")), # as in revision
                        bold(str(revision)),
                ),
            importance = 1,
            type = "info",
            header = red(" @@ ")
        )

        download_url = self._setup_repository_package_filename(idpackage, repo = repo)
        downloadfile = os.path.basename(download_url)
        destination_path = os.path.join(upload_dir,downloadfile)
        shutil.move(package_file,destination_path)

        dbconn.commitChanges()
        return idpackage,destination_path

    # this function changes the final repository package filename
    def _setup_repository_package_filename(self, idpackage, repo = None):

        dbconn = self.openServerDatabase(read_only = False, no_upload = True, repo = repo)

        downloadurl = dbconn.retrieveDownloadURL(idpackage)
        packagerev = dbconn.retrieveRevision(idpackage)
        downloaddir = os.path.dirname(downloadurl)
        downloadfile = os.path.basename(downloadurl)
        # add revision
        downloadfile = downloadfile[:-5]+"~%s%s" % (packagerev,etpConst['packagesext'],)
        downloadurl = os.path.join(downloaddir,downloadfile)

        # update url
        dbconn.setDownloadURL(idpackage,downloadurl)

        return downloadurl

    def add_packages_to_repository(self, packages_data, ask = True, repo = None):

        if repo == None:
            repo = self.default_repository

        mycount = 0
        maxcount = len(packages_data)
        idpackages_added = set()
        to_be_injected = set()
        myQA = self.QA()
        missing_deps_taint = False
        for packagedata in packages_data:

            mycount += 1
            package_filepath = packagedata[0]
            requested_branch = packagedata[1]
            inject = packagedata[2]
            self.updateProgress(
                "[repo:%s] %s: %s" % (
                            darkgreen(repo),
                            blue(_("adding package")),
                            darkgreen(os.path.basename(package_filepath)),
                        ),
                importance = 1,
                type = "info",
                header = blue(" @@ "),
                count = (mycount,maxcount,)
            )

            try:
                # add to database
                idpackage, destination_path = self.package_injector(
                                    package_filepath,
                                    branch = requested_branch,
                                    inject = inject
                )
                idpackages_added.add(idpackage)
                to_be_injected.add((idpackage,destination_path))
            except Exception, e:
                self.updateProgress(
                    "[repo:%s] %s: %s" % (
                                darkgreen(repo),
                                darkred(_("Exception caught, running injection and RDEPEND check before raising")),
                                darkgreen(str(e)),
                            ),
                    importance = 1,
                    type = "error",
                    header = bold(" !!! "),
                    count = (mycount,maxcount,)
                )
                # reinit depends table
                self.depends_table_initialize(repo)
                if idpackages_added:
                    dbconn = self.openServerDatabase(read_only = False, no_upload = True, repo = repo)
                    missing_deps_taint = myQA.scan_missing_dependencies(
                        idpackages_added,
                        dbconn,
                        ask = ask,
                        repo = repo,
                        self_check = True
                    )
                    myQA.test_depends_linking(idpackages_added, dbconn, repo = repo)
                if to_be_injected:
                    self.inject_database_into_packages(to_be_injected, repo = repo)
                # reinit depends table
                if missing_deps_taint:
                    self.depends_table_initialize(repo)
                self.close_server_databases()
                raise

        # reinit depends table
        self.depends_table_initialize(repo)

        if idpackages_added:
            dbconn = self.openServerDatabase(read_only = False, no_upload = True, repo = repo)
            missing_deps_taint = myQA.scan_missing_dependencies(
                idpackages_added,
                dbconn,
                ask = ask,
                repo = repo,
                self_check = True
            )
            myQA.test_depends_linking(idpackages_added, dbconn, repo = repo)

        # reinit depends table
        if missing_deps_taint:
            self.depends_table_initialize(repo)

        # inject database into packages
        self.inject_database_into_packages(to_be_injected, repo = repo)

        return idpackages_added


    def inject_database_into_packages(self, injection_data, repo = None):

        if repo == None:
            repo = self.default_repository

        # now inject metadata into tbz2 packages
        self.updateProgress(
            "[repo:%s] %s:" % (
                        darkgreen(repo),
                        blue(_("Injecting entropy metadata into built packages")),
                    ),
            importance = 1,
            type = "info",
            header = red(" @@ ")
        )

        dbconn = self.openServerDatabase(read_only = False, no_upload = True, repo = repo)
        for pkgdata in injection_data:
            idpackage = pkgdata[0]
            package_path = pkgdata[1]
            self.updateProgress(
                "[repo:%s|%s] %s: %s" % (
                            darkgreen(repo),
                            brown(str(idpackage)),
                            blue(_("injecting entropy metadata")),
                            darkgreen(os.path.basename(package_path)),
                        ),
                importance = 1,
                type = "info",
                header = blue(" @@ "),
                back = True
            )
            data = dbconn.getPackageData(idpackage)
            dbpath = self.ClientService.inject_entropy_database_into_package(package_path, data)
            digest = self.entropyTools.md5sum(package_path)
            # update digest
            dbconn.setDigest(idpackage,digest)
            self.entropyTools.createHashFile(package_path)
            # remove garbage
            os.remove(dbpath)
            self.updateProgress(
                "[repo:%s|%s] %s: %s" % (
                            darkgreen(repo),
                            brown(str(idpackage)),
                            blue(_("injection complete")),
                            darkgreen(os.path.basename(package_path)),
                        ),
                importance = 1,
                type = "info",
                header = red(" @@ ")
            )
            dbconn.commitChanges()

    def quickpkg(self, atom,storedir):
        return self.SpmService.quickpkg(atom,storedir)


    def remove_packages(self, idpackages, repo = None):

        if repo == None:
            repo = self.default_repository

        dbconn = self.openServerDatabase(read_only = False, no_upload = True, repo = repo)
        for idpackage in idpackages:
            atom = dbconn.retrieveAtom(idpackage)
            self.updateProgress(
                "[repo:%s] %s: %s" % (
                        darkgreen(repo),
                        blue(_("removing package")),
                        darkgreen(atom),
                ),
                importance = 1,
                type = "info",
                header = brown(" @@ ")
            )
            dbconn.removePackage(idpackage)
        self.close_server_database(dbconn)
        self.updateProgress(
            "[repo:%s] %s" % (
                        darkgreen(repo),
                        blue(_("removal complete")),
                ),
            importance = 1,
            type = "info",
            header = brown(" @@ ")
        )


    def bump_database(self, repo = None):
        dbconn = self.openServerDatabase(read_only = False, no_upload = True, repo = repo)
        dbconn.taintDatabase()
        self.close_server_database(dbconn)

    def get_remote_mirrors(self, repo = None):
        if repo == None:
            repo = self.default_repository
        return etpConst['server_repositories'][repo]['mirrors']

    def get_remote_packages_relative_path(self, repo = None):
        if repo == None:
            repo = self.default_repository
        return etpConst['server_repositories'][repo]['packages_relative_path']

    def get_remote_database_relative_path(self, repo = None):
        if repo == None:
            repo = self.default_repository
        return etpConst['server_repositories'][repo]['database_relative_path']

    def get_local_database_file(self, repo = None):
        if repo == None:
            repo = self.default_repository
        return os.path.join(etpConst['server_repositories'][repo]['database_dir'],etpConst['etpdatabasefile'])

    def get_local_store_directory(self, repo = None):
        if repo == None:
            repo = self.default_repository
        return etpConst['server_repositories'][repo]['store_dir']

    def get_local_upload_directory(self, repo = None):
        if repo == None:
            repo = self.default_repository
        return etpConst['server_repositories'][repo]['upload_dir']

    def get_local_packages_directory(self, repo = None):
        if repo == None:
            repo = self.default_repository
        return etpConst['server_repositories'][repo]['packages_dir']

    def get_local_database_taint_file(self, repo = None):
        if repo == None:
            repo = self.default_repository
        return os.path.join(self.get_local_database_dir(repo),etpConst['etpdatabasetaintfile'])

    def get_local_database_revision_file(self, repo = None):
        if repo == None:
            repo = self.default_repository
        return os.path.join(self.get_local_database_dir(repo),etpConst['etpdatabaserevisionfile'])

    def get_local_database_mask_file(self, repo = None):
        if repo == None:
            repo = self.default_repository
        return os.path.join(self.get_local_database_dir(repo),etpConst['etpdatabasemaskfile'])

    def get_local_database_licensewhitelist_file(self, repo = None):
        if repo == None:
            repo = self.default_repository
        return os.path.join(self.get_local_database_dir(repo),etpConst['etpdatabaselicwhitelistfile'])

    def get_local_database_rss_file(self, repo = None):
        if repo == None:
            repo = self.default_repository
        return os.path.join(self.get_local_database_dir(repo),etpConst['rss-name'])

    def get_local_database_rsslight_file(self, repo = None):
        if repo == None:
            repo = self.default_repository
        return os.path.join(self.get_local_database_dir(repo),etpConst['rss-light-name'])

    def get_local_database_treeupdates_file(self, repo = None):
        if repo == None:
            repo = self.default_repository
        return os.path.join(self.get_local_database_dir(repo),etpConst['etpdatabaseupdatefile'])

    def get_local_database_dir(self, repo = None):
        if repo == None:
            repo = self.default_repository
        return etpConst['server_repositories'][repo]['database_dir']

    def get_local_database_revision(self, repo = None):

        if repo == None:
            repo = self.default_repository

        dbrev_file = self.get_local_database_revision_file(repo)
        if os.path.isfile(dbrev_file):
            f = open(dbrev_file)
            rev = f.readline().strip()
            f.close()
            try:
                rev = int(rev)
            except ValueError:
                self.updateProgress(
                    "[repo:%s] %s: %s - %s" % (
                            darkgreen(repo),
                            blue(_("invalid database revision")),
                            bold(rev),
                            blue(_("defaulting to 0")),
                        ),
                    importance = 2,
                    type = "error",
                    header = darkred(" !!! ")
                )
                rev = 0
            return rev
        else:
            return 0

    def atomMatch(self, *args, **kwargs):
        repos = etpConst['server_repositories'].keys()
        kwargs['server_repos'] = repos
        kwargs['serverInstance'] = self
        return self.ClientService.atomMatch(*args,**kwargs)

    def scan_package_changes(self):

        installed_packages = self.SpmService.get_installed_packages_counter()
        installed_counters = set()
        toBeAdded = set()
        toBeRemoved = set()
        toBeInjected = set()

        server_repos = etpConst['server_repositories'].keys()

        # packages to be added
        for x in installed_packages:
            found = False
            for server_repo in server_repos:
                installed_counters.add(x[1])
                server_dbconn = self.openServerDatabase(read_only = True, no_upload = True, repo = server_repo)
                counter = server_dbconn.isCounterAvailable(x[1], branch = etpConst['branch'], branch_operator = "<=")
                if counter:
                    found = True
                    break
            if not found:
                toBeAdded.add(tuple(x))

        # packages to be removed from the database
        database_counters = {}
        for server_repo in server_repos:
            server_dbconn = self.openServerDatabase(read_only = True, no_upload = True, repo = server_repo)
            database_counters[server_repo] = \
                    server_dbconn.listAllCounters(
                                                    branch = etpConst['branch'],
                                                    branch_operator = "<="
                                                 )

        ordered_counters = set()
        for server_repo in database_counters:
            for data in database_counters[server_repo]:
                ordered_counters.add((data,server_repo))
        database_counters = ordered_counters

        for x in database_counters:

            xrepo = x[1]
            x = x[0]

            if x[0] < 0:
                continue # skip packages without valid counter

            if x[0] in installed_counters:
                continue

            dbconn = self.openServerDatabase(read_only = True, no_upload = True, repo = xrepo)

            dorm = True
            # check if the package is in toBeAdded
            if toBeAdded:

                dorm = False
                atom = dbconn.retrieveAtom(x[1])
                atomkey = self.entropyTools.dep_getkey(atom)
                atomtag = self.entropyTools.dep_gettag(atom)
                atomslot = dbconn.retrieveSlot(x[1])

                add = True
                for pkgdata in toBeAdded:
                    addslot = self.SpmService.get_package_slot(pkgdata[0])
                    addkey = self.entropyTools.dep_getkey(pkgdata[0])
                    # workaround for ebuilds not having slot
                    if addslot == None:
                        addslot = '0'                                              # handle tagged packages correctly
                    if (atomkey == addkey) and ((str(atomslot) == str(addslot)) or (atomtag != None)):
                        # do not add to toBeRemoved
                        add = False
                        break

                if not add:
                    continue
                dorm = True

            if dorm:
                trashed = self.is_counter_trashed(x[0])
                if trashed:
                    # search into portage then
                    key, slot = dbconn.retrieveKeySlot(x[1])
                    trashed = self.SpmService.get_installed_atom(key+":"+slot)
                if not trashed:
                    dbtag = dbconn.retrieveVersionTag(x[1])
                    if dbtag != '':
                        is_injected = dbconn.isInjected(x[1])
                        if not is_injected:
                            toBeInjected.add((x[1],xrepo))
                    else:
                        toBeRemoved.add((x[1],xrepo))

        return toBeAdded, toBeRemoved, toBeInjected

    def is_counter_trashed(self, counter):
        server_repos = etpConst['server_repositories'].keys()
        for repo in server_repos:
            dbconn = self.openServerDatabase(read_only = True, no_upload = True, repo = repo)
            if dbconn.isCounterTrashed(counter):
                return True
        return False

    def transform_package_into_injected(self, idpackage, repo = None):
        dbconn = self.openServerDatabase(read_only = False, no_upload = True, repo = repo)
        counter = dbconn.getNewNegativeCounter()
        dbconn.setCounter(idpackage,counter)
        dbconn.setInjected(idpackage)

    def initialize_server_database(self, empty = True, repo = None, warnings = True):

        if repo == None:
            repo = self.default_repository

        self.close_server_databases()
        revisions_match = {}
        treeupdates_actions = []
        injected_packages = set()
        idpackages = set()
        idpackages_added = set()

        mytxt = red("%s ...") % (_("Initializing Entropy database"),)
        self.updateProgress(
            mytxt,
            importance = 1,
            type = "info",
            header = darkgreen(" * "),
            back = True
        )

        if os.path.isfile(self.get_local_database_file(repo)):

            dbconn = self.openServerDatabase(read_only = True, no_upload = True, repo = repo, warnings = warnings)

            if dbconn.doesTableExist("baseinfo") and dbconn.doesTableExist("extrainfo"):
                idpackages = dbconn.listAllIdpackages()

            if dbconn.doesTableExist("treeupdatesactions"):
                treeupdates_actions = dbconn.listAllTreeUpdatesActions()

            # save list of injected packages
            if dbconn.doesTableExist("injected") and dbconn.doesTableExist("extrainfo"):
                injected_packages = dbconn.listAllInjectedPackages(justFiles = True)
                injected_packages = set([os.path.basename(x) for x in injected_packages])

            for idpackage in idpackages:
                package = os.path.basename(dbconn.retrieveDownloadURL(idpackage))
                branch = dbconn.retrieveBranch(idpackage)
                revision = dbconn.retrieveRevision(idpackage)
                revisions_match[package] = (branch,revision,)

            self.close_server_database(dbconn)

            mytxt = "%s: %s: %s" % (
                bold(_("WARNING")),
                red(_("database already exists")),
                self.get_local_database_file(repo),
            )
            self.updateProgress(
                mytxt,
                importance = 1,
                type = "warning",
                header = darkred(" !!! ")
            )

            rc = self.askQuestion(_("Do you want to continue ?"))
            if rc == "No":
                return
            os.remove(self.get_local_database_file(repo))


        # initialize
        dbconn = self.openServerDatabase(read_only = False, no_upload = True, repo = repo)
        dbconn.initializeDatabase()

        if not empty:

            revisions_file = "/entropy-revisions-dump.txt"
            # dump revisions - as a backup
            if revisions_match:
                self.updateProgress(
                    "%s: %s" % (
                        red(_("Dumping current revisions to file")),
                        darkgreen(revisions_file),
                    ),
                    importance = 1,
                    type = "info",
                    header = darkgreen(" * ")
                )
                f = open(revisions_file,"w")
                f.write(str(revisions_match))
                f.flush()
                f.close()

            # dump treeupdates - as a backup
            treeupdates_file = "/entropy-treeupdates-dump.txt"
            if treeupdates_actions:
                self.updateProgress(
                    "%s: %s" % (
                        red(_("Dumping current 'treeupdates' actions to file")), # do not translate treeupdates
                        bold(treeupdates_file),
                    ),
                    importance = 1,
                    type = "info",
                    header = darkgreen(" * ")
                )
                f = open(treeupdates_file,"w")
                f.write(str(treeupdates_actions))
                f.flush()
                f.close()

            rc = self.askQuestion(_("Would you like to sync packages first (important if you don't have them synced) ?"))
            if rc == "Yes":
                self.MirrorsService.sync_packages(repo = repo)

            # fill tree updates actions
            if treeupdates_actions:
                dbconn.bumpTreeUpdatesActions(treeupdates_actions)

            # now fill the database
            pkgbranches = etpConst['branches']

            for mybranch in pkgbranches:

                pkg_branch_dir = os.path.join(self.get_local_packages_directory(repo),mybranch)
                pkglist = os.listdir(pkg_branch_dir)
                # filter .md5 and .expired packages
                pkglist = [x for x in pkglist if x[-5:] == etpConst['packagesext'] and not \
                    os.path.isfile(os.path.join(pkg_branch_dir,x+etpConst['packagesexpirationfileext']))]

                if not pkglist:
                    continue

                self.updateProgress(
                    "%s '%s' %s %s" % (
                        red(_("Reinitializing Entropy database for branch")),
                        bold(mybranch),
                        red(_("using Packages in the repository")),
                        red("..."),
                    ),
                    importance = 1,
                    type = "info",
                    header = darkgreen(" * ")
                )

                counter = 0
                maxcount = len(pkglist)
                for pkg in pkglist:
                    counter += 1

                    self.updateProgress(
                        "[repo:%s|%s] %s: %s" % (
                                darkgreen(repo),
                                brown(mybranch),
                                blue(_("analyzing")),
                                bold(pkg),
                            ),
                        importance = 1,
                        type = "info",
                        header = " ",
                        back = True,
                        count = (counter,maxcount,)
                    )

                    doinject = False
                    if pkg in injected_packages:
                        doinject = True

                    pkg_path = os.path.join(self.get_local_packages_directory(repo),mybranch,pkg)
                    mydata = self.ClientService.extract_pkg_metadata(pkg_path, mybranch, inject = doinject)

                    # get previous revision
                    revision_avail = revisions_match.get(pkg)
                    addRevision = 0
                    if (revision_avail != None):
                        if mybranch == revision_avail[0]:
                            addRevision = revision_avail[1]

                    idpackage, revision, mydata_upd = dbconn.addPackage(mydata, revision = addRevision)
                    idpackages_added.add(idpackage)

                    self.updateProgress(
                        "[repo:%s] [%s:%s/%s] %s: %s, %s: %s" % (
                                    repo,
                                    brown(mybranch),
                                    darkgreen(counter),
                                    blue(maxcount),
                                    red(_("added package")),
                                    darkgreen(pkg),
                                    red(_("revision")),
                                    brown(mydata_upd['revision']),
                            ),
                        importance = 1,
                        type = "info",
                        header = " ",
                        back = True
                    )

            self.depends_table_initialize(repo)

            myQA = self.QA()

            if idpackages_added:
                dbconn = self.openServerDatabase(read_only = False, no_upload = True, repo = repo)
                myQA.scan_missing_dependencies(idpackages_added, dbconn, ask = True, repo = repo, self_check = True)

        dbconn.commitChanges()
        self.close_server_databases()

        return 0

    def match_packages(self, packages, repo = None):

        dbconn = self.openServerDatabase(read_only = True, no_upload = True, repo = repo)
        if ("world" in packages) or not packages:
            return dbconn.listAllIdpackages(),True
        else:
            idpackages = set()
            for package in packages:
                matches = dbconn.atomMatch(package, multiMatch = True, matchBranches = etpConst['branches'])
                if matches[1] == 0:
                    idpackages |= matches[0]
                else:
                    mytxt = "%s: %s: %s" % (red(_("Attention")),blue(_("cannot match")),bold(package),)
                    self.updateProgress(
                        mytxt,
                        importance = 1,
                        type = "warning",
                        header = darkred(" !!! ")
                    )
            return idpackages,False

    def get_remote_package_checksum(self, repo, filename, branch):

        if not etpConst['server_repositories'][repo].has_key('handler'):
            return None
        url = etpConst['server_repositories'][repo]['handler']

        # does the package has "#" (== tag) ? hackish thing that works
        filename = filename.replace("#","%23")
        # "+"
        filename = filename.replace("+","%2b")
        request = os.path.join(url,etpConst['handlers']['md5sum'])
        request += filename+"&branch="+branch

        # now pray the server
        try:
            if etpConst['proxy']:
                proxy_support = urllib2.ProxyHandler(etpConst['proxy'])
                opener = urllib2.build_opener(proxy_support)
                urllib2.install_opener(opener)
            item = urllib2.urlopen(request)
            result = item.readline().strip()
            item.close()
            del item
            return result
        except: # no HTTP support?
            return None

    def verify_remote_packages(self, packages, ask = True, repo = None):

        if repo == None:
            repo = self.default_repository

        self.updateProgress(
            "[%s] %s:" % (
                red("remote"),
                blue(_("Integrity verification of the selected packages")),
            ),
            importance = 1,
            type = "info",
            header = blue(" @@ ")
        )

        idpackages, world = self.match_packages(packages)
        dbconn = self.openServerDatabase(read_only = True, no_upload = True, repo = repo)

        if world:
            self.updateProgress(
                blue(_("All the packages in the Entropy Packages repository will be checked.")),
                importance = 1,
                type = "info",
                header = "    "
            )
        else:
            mytxt = red("%s:") % (_("This is the list of the packages that would be checked"),)
            self.updateProgress(
                mytxt,
                importance = 1,
                type = "info",
                header = "    "
            )
            for idpackage in idpackages:
                pkgatom = dbconn.retrieveAtom(idpackage)
                pkgbranch = dbconn.retrieveBranch(idpackage)
                pkgfile = os.path.basename(dbconn.retrieveDownloadURL(idpackage))
                self.updateProgress(
                    red(pkgatom)+" -> "+bold(os.path.join(pkgbranch,pkgfile)),
                    importance = 1,
                    type = "info",
                    header = darkgreen("   - ")
                )

        if ask:
            rc = self.askQuestion(_("Would you like to continue ?"))
            if rc == "No":
                return set(),set(),{}

        match = set()
        not_match = set()
        broken_packages = {}

        for uri in self.get_remote_mirrors(repo):

            crippled_uri = self.entropyTools.extractFTPHostFromUri(uri)
            self.updateProgress(
                "[repo:%s] %s: %s" % (
                        darkgreen(repo),
                        blue(_("Working on mirror")),
                        brown(crippled_uri),
                    ),
                importance = 1,
                type = "info",
                header = red(" @@ ")
            )


            totalcounter = len(idpackages)
            currentcounter = 0
            for idpackage in idpackages:

                currentcounter += 1
                pkgfile = dbconn.retrieveDownloadURL(idpackage)
                pkgbranch = dbconn.retrieveBranch(idpackage)
                pkgfilename = os.path.basename(pkgfile)

                self.updateProgress(
                    "[%s] %s: %s" % (
                            brown(crippled_uri),
                            blue(_("checking hash")),
                            darkgreen(os.path.join(pkgbranch,pkgfilename)),
                    ),
                    importance = 1,
                    type = "info",
                    header = blue(" @@ "),
                    back = True,
                    count = (currentcounter,totalcounter,)
                )

                ckOk = False
                ck = self.get_remote_package_checksum(repo, pkgfilename, pkgbranch)
                if ck == None:
                    self.updateProgress(
                        "[%s] %s: %s %s" % (
                            brown(crippled_uri),
                            blue(_("digest verification of")),
                            bold(pkgfilename),
                            blue(_("not supported")),
                        ),
                        importance = 1,
                        type = "info",
                        header = blue(" @@ "),
                        count = (currentcounter,totalcounter,)
                    )
                elif len(ck) == 32:
                    ckOk = True
                else:
                    self.updateProgress(
                        "[%s] %s: %s %s" % (
                            brown(crippled_uri),
                            blue(_("digest verification of")),
                            bold(pkgfilename),
                            blue(_("failed for unknown reasons")),
                        ),
                        importance = 1,
                        type = "info",
                        header = blue(" @@ "),
                        count = (currentcounter,totalcounter,)
                    )

                if ckOk:
                    match.add(idpackage)
                else:
                    not_match.add(idpackage)
                    self.updateProgress(
                        "[%s] %s: %s %s" % (
                            brown(crippled_uri),
                            blue(_("package")),
                            bold(pkgfilename),
                            red(_("NOT healthy")),
                        ),
                        importance = 1,
                        type = "warning",
                        header = darkred(" !!! "),
                        count = (currentcounter,totalcounter,)
                    )
                    if not broken_packages.has_key(crippled_uri):
                        broken_packages[crippled_uri] = []
                    broken_packages[crippled_uri].append(os.path.join(pkgbranch,pkgfilename))

            if broken_packages:
                mytxt = blue("%s:") % (_("This is the list of broken packages"),)
                self.updateProgress(
                    mytxt,
                    importance = 1,
                    type = "info",
                    header = red(" * ")
                )
                for mirror in broken_packages.keys():
                    mytxt = "%s: %s" % (brown(_("Mirror")),bold(mirror),)
                    self.updateProgress(
                        mytxt,
                        importance = 1,
                        type = "info",
                        header = red("   <> ")
                    )
                    for bp in broken_packages[mirror]:
                        self.updateProgress(
                            blue(bp),
                            importance = 1,
                            type = "info",
                            header = red("      - ")
                        )

            self.updateProgress(
                "%s:" % (
                    blue(_("Statistics")),
                ),
                importance = 1,
                type = "info",
                header = red(" @@ ")
            )
            self.updateProgress(
                "[%s] %s:\t%s" % (
                    red(crippled_uri),
                    brown(_("Number of checked packages")),
                    brown(str(len(match)+len(not_match))),
                ),
                importance = 1,
                type = "info",
               header = brown("   # ")
            )
            self.updateProgress(
                "[%s] %s:\t%s" % (
                    red(crippled_uri),
                    darkgreen(_("Number of healthy packages")),
                    darkgreen(str(len(match))),
                ),
                importance = 1,
                type = "info",
               header = brown("   # ")
            )
            self.updateProgress(
                "[%s] %s:\t%s" % (
                    red(crippled_uri),
                    darkred(_("Number of broken packages")),
                    darkred(str(len(not_match))),
                ),
                importance = 1,
                type = "info",
                header = brown("   # ")
            )

        return match,not_match,broken_packages


    def verify_local_packages(self, packages, ask = True, repo = None):

        if repo == None:
            repo = self.default_repository

        self.updateProgress(
            "[%s] %s:" % (
                red(_("local")),
                blue(_("Integrity verification of the selected packages")),
            ),
            importance = 1,
            type = "info",
            header = darkgreen(" * ")
        )

        idpackages, world = self.match_packages(packages)
        dbconn = self.openServerDatabase(read_only = True, no_upload = True, repo = repo)

        if world:
            self.updateProgress(
                blue(_("All the packages in the Entropy Packages repository will be checked.")),
                importance = 1,
                type = "info",
                header = "    "
            )

        to_download = set()
        available = set()
        for idpackage in idpackages:

            pkgatom = dbconn.retrieveAtom(idpackage)
            pkgbranch = dbconn.retrieveBranch(idpackage)
            pkgfile = dbconn.retrieveDownloadURL(idpackage)
            pkgfile = os.path.basename(pkgfile)

            bindir_path = os.path.join(self.get_local_packages_directory(repo),pkgbranch,pkgfile)
            uploaddir_path = os.path.join(self.get_local_upload_directory(repo),pkgbranch,pkgfile)

            if os.path.isfile(bindir_path):
                if not world:
                    self.updateProgress(
                        "[%s] %s :: %s" % (
                                darkgreen(_("available")),
                                blue(pkgatom),
                                darkgreen(pkgfile),
                        ),
                        importance = 0,
                        type = "info",
                        header = darkgreen("   # ")
                    )
                available.add(idpackage)
            elif os.path.isfile(uploaddir_path):
                if not world:
                    self.updateProgress(
                        "[%s] %s :: %s" % (
                                darkred(_("upload/ignored")),
                                blue(pkgatom),
                                darkgreen(pkgfile),
                        ),
                        importance = 0,
                        type = "info",
                        header = darkgreen("   # ")
                    )
            else:
                self.updateProgress(
                    "[%s] %s :: %s" % (
                            brown(_("download")),
                            blue(pkgatom),
                            darkgreen(pkgfile),
                    ),
                    importance = 0,
                    type = "info",
                    header = darkgreen("   # ")
                )
                to_download.add((idpackage,pkgfile,pkgbranch))

        if ask:
            rc = self.askQuestion(_("Would you like to continue ?"))
            if rc == "No":
                return set(),set(),set(),set()


        fine = set()
        failed = set()
        downloaded_fine = set()
        downloaded_errors = set()

        if to_download:

            not_downloaded = set()
            mytxt = blue("%s ...") % (_("Starting to download missing files"),)
            self.updateProgress(
                mytxt,
                importance = 1,
                type = "info",
                header = "   "
            )
            for uri in self.get_remote_mirrors(repo):

                if not_downloaded:
                    mytxt = blue("%s ...") % (_("Trying to search missing or broken files on another mirror"),)
                    self.updateProgress(
                        mytxt,
                        importance = 1,
                        type = "info",
                        header = "   "
                    )
                    to_download = not_downloaded.copy()
                    not_downloaded = set()

                for pkg in to_download:
                    rc = self.MirrorsService.download_package(uri,pkg[1],pkg[2], repo = repo)
                    if rc == None:
                        not_downloaded.add((pkg[1],pkg[2]))
                    elif not rc:
                        not_downloaded.add((pkg[1],pkg[2]))
                    elif rc:
                        downloaded_fine.add(pkg[0])
                        available.add(pkg[0])

                if not not_downloaded:
                    self.updateProgress(
                        red(_("All the binary packages have been downloaded successfully.")),
                        importance = 1,
                        type = "info",
                        header = "   "
                    )
                    break

            if not_downloaded:
                mytxt = blue("%s:") % (_("These are the packages that cannot be found online"),)
                self.updateProgress(
                    mytxt,
                    importance = 1,
                    type = "info",
                    header = "   "
                )
                for i in not_downloaded:
                    downloaded_errors.add(i[0])
                    self.updateProgress(
                            brown(i[0])+" in "+blue(i[1]),
                            importance = 1,
                            type = "warning",
                            header = red("    * ")
                    )
                    downloaded_errors.add(i[0])
                mytxt = "%s." % (_("They won't be checked"),)
                self.updateProgress(
                    mytxt,
                    importance = 1,
                    type = "warning",
                    header = "   "
                )

        totalcounter = str(len(available))
        currentcounter = 0
        for idpackage in available:
            currentcounter += 1
            pkgfile = dbconn.retrieveDownloadURL(idpackage)
            pkgbranch = dbconn.retrieveBranch(idpackage)
            pkgfile = os.path.basename(pkgfile)

            self.updateProgress(
                "[branch:%s] %s %s" % (
                        brown(pkgbranch),
                        blue(_("checking hash of")),
                        darkgreen(pkgfile),
                ),
                importance = 1,
                type = "info",
                header = "   ",
                back = True,
                count = (currentcounter,totalcounter,)
            )

            storedmd5 = dbconn.retrieveDigest(idpackage)
            pkgpath = os.path.join(self.get_local_packages_directory(repo),pkgbranch,pkgfile)
            result = self.entropyTools.compareMd5(pkgpath,storedmd5)
            if result:
                fine.add(idpackage)
            else:
                failed.add(idpackage)
                self.updateProgress(
                    "[branch:%s] %s %s %s: %s" % (
                            brown(pkgbranch),
                            blue(_("package")),
                            darkgreen(pkgfile),
                            blue(_("is corrupted, stored checksum")), # package -blah- is corrupted...
                            brown(storedmd5),
                    ),
                    importance = 1,
                    type = "info",
                    header = "   ",
                    count = (currentcounter,totalcounter,)
                )

        if failed:
            mytxt = blue("%s:") % (_("This is the list of broken packages"),)
            self.updateProgress(
                    mytxt,
                    importance = 1,
                    type = "warning",
                    header =  darkred("  # ")
            )
            for idpackage in failed:
                branch = dbconn.retrieveBranch(idpackage)
                dp = os.path.basename(dbconn.retrieveDownloadURL(idpackage))
                self.updateProgress(
                        blue("[branch:%s] %s" % (branch,dp,)),
                        importance = 0,
                        type = "warning",
                        header =  brown("    # ")
                )

        # print stats
        self.updateProgress(
            red("Statistics:"),
            importance = 1,
            type = "info",
            header = blue(" * ")
        )
        self.updateProgress(
            brown("%s:\t\t%s" % (
                    _("Number of checked packages"),
                    len(fine)+len(failed),
                )
            ),
            importance = 0,
            type = "info",
            header = brown("   # ")
        )
        self.updateProgress(
            darkgreen("%s:\t\t%s" % (
                    _("Number of healthy packages"),
                    len(fine),
                )
            ),
            importance = 0,
            type = "info",
            header = brown("   # ")
        )
        self.updateProgress(
            darkred("%s:\t\t%s" % (
                    _("Number of broken packages"),
                    len(failed),
                )
            ),
            importance = 0,
            type = "info",
            header = brown("   # ")
        )
        self.updateProgress(
            blue("%s:\t\t%s" % (
                    _("Number of downloaded packages"),
                    len(downloaded_fine),
                )
            ),
            importance = 0,
            type = "info",
            header = brown("   # ")
        )
        self.updateProgress(
            bold("%s:\t\t%s" % (
                    _("Number of failed downloads"),
                    len(downloaded_errors),
                )
            ),
            importance = 0,
            type = "info",
            header = brown("   # ")
        )

        self.close_server_database(dbconn)
        return fine, failed, downloaded_fine, downloaded_errors


    def list_all_branches(self, repo = None):
        dbconn = self.openServerDatabase(just_reading = True, repo = repo)
        branches = dbconn.listAllBranches()
        for branch in branches:
            branch_path = os.path.join(self.get_local_upload_directory(repo),branch)
            if not os.path.isdir(branch_path):
                os.makedirs(branch_path)
            branch_path = os.path.join(self.get_local_packages_directory(repo),branch)
            if not os.path.isdir(branch_path):
                os.makedirs(branch_path)

    def switch_packages_branch(self, idpackages, to_branch, repo = None):

        if repo == None:
            repo = self.default_repository

        mytxt = red("%s ...") % (_("Switching selected packages"),)
        self.updateProgress(
            mytxt,
            importance = 1,
            type = "info",
            header = darkgreen(" @@ ")
        )
        dbconn = self.openServerDatabase(read_only = False, no_upload = True, repo = repo)

        already_switched = set()
        not_found = set()
        switched = set()
        ignored = set()
        no_checksum = set()

        for idpackage in idpackages:

            cur_branch = dbconn.retrieveBranch(idpackage)
            atom = dbconn.retrieveAtom(idpackage)
            if cur_branch == to_branch:
                already_switched.add(idpackage)
                self.updateProgress(
                    red("%s %s, %s %s" % (
                            _("Ignoring"),
                            bold(atom),
                            _("already in branch"),
                            cur_branch,
                        )
                    ),
                    importance = 0,
                    type = "info",
                    header = darkgreen(" @@ ")
                )
                ignored.add(idpackage)
                continue
            old_filename = os.path.basename(dbconn.retrieveDownloadURL(idpackage))
            # check if file exists
            frompath = os.path.join(self.get_local_packages_directory(repo),cur_branch,old_filename)
            if not os.path.isfile(frompath):
                self.updateProgress(
                    "[%s=>%s] %s, %s" % (
                        brown(cur_branch),
                        bold(to_branch),
                        darkgreen(atom),
                        blue(_("cannot switch, package not found!")),
                    ),
                    importance = 0,
                    type = "warning",
                    header = darkred(" !!! ")
                )
                not_found.add(idpackage)
                continue

            mytxt = blue("%s ...") % (_("configuring package information"),)
            self.updateProgress(
                "[%s=>%s] %s, %s" % (
                    brown(cur_branch),
                    bold(to_branch),
                    darkgreen(atom),
                    mytxt,
                ),
                importance = 0,
                type = "info",
                header = darkgreen(" @@ "),
                back = True
            )
            dbconn.switchBranch(idpackage,to_branch)
            dbconn.commitChanges()

            mytxt = blue("%s ...") % (_("moving file locally"),)
            # LOCAL
            self.updateProgress(
                "[%s=>%s] %s, %s" % (
                        brown(cur_branch),
                        bold(to_branch),
                        darkgreen(atom),
                        mytxt,
                ),
                importance = 0,
                type = "info",
                header = darkgreen(" @@ "),
                back = True
            )
            new_filename = os.path.basename(dbconn.retrieveDownloadURL(idpackage))
            topath = os.path.join(self.get_local_packages_directory(repo),to_branch)
            if not os.path.isdir(topath):
                os.makedirs(topath)

            topath = os.path.join(topath,new_filename)
            shutil.move(frompath,topath)
            if os.path.isfile(frompath+etpConst['packageshashfileext']):
                shutil.move(frompath+etpConst['packageshashfileext'],topath+etpConst['packageshashfileext'])
            else:
                self.updateProgress(
                    "[%s=>%s] %s, %s" % (
                            brown(cur_branch),
                            bold(to_branch),
                            darkgreen(atom),
                            blue(_("cannot find checksum to migrate!")),
                    ),
                    importance = 0,
                    type = "warning",
                    header = darkred(" !!! ")
                )
                no_checksum.add(idpackage)

            mytxt = blue("%s ...") % (_("moving file remotely"),)
            # REMOTE
            self.updateProgress(
                "[%s=>%s] %s, %s" % (
                        brown(cur_branch),
                        bold(to_branch),
                        darkgreen(atom),
                        mytxt,
                ),
                importance = 0,
                type = "info",
                header = darkgreen(" @@ "),
                back = True
            )

            for uri in self.get_remote_mirrors(repo):

                crippled_uri = self.entropyTools.extractFTPHostFromUri(uri)
                self.updateProgress(
                    "[%s=>%s] %s, %s: %s" % (
                            brown(cur_branch),
                            bold(to_branch),
                            darkgreen(atom),
                            blue(_("moving file remotely on")), # on... servername
                            darkgreen(crippled_uri),
                    ),
                    importance = 0,
                    type = "info",
                    header = darkgreen(" @@ "),
                    back = True
                )

                ftp = self.FtpInterface(uri, self)
                ftp.setCWD(self.get_remote_packages_relative_path(repo))
                # create directory if it doesn't exist
                if not ftp.isFileAvailable(to_branch):
                    ftp.mkdir(to_branch)

                fromuri = os.path.join(cur_branch,old_filename)
                touri = os.path.join(to_branch,new_filename)
                ftp.renameFile(fromuri,touri)
                ftp.renameFile(fromuri+etpConst['packageshashfileext'],touri+etpConst['packageshashfileext'])
                ftp.closeConnection()

            switched.add(idpackage)

        dbconn.commitChanges()
        self.close_server_database(dbconn)
        mytxt = blue("%s.") % (_("migration loop completed"),)
        self.updateProgress(
            "[%s=>%s] %s" % (
                    brown(cur_branch),
                    bold(to_branch),
                    mytxt,
            ),
            importance = 1,
            type = "info",
            header = darkgreen(" * ")
        )

        return switched, already_switched, ignored, not_found, no_checksum

class RepositorySocketServerInterface(SocketHostInterface):

    class RepositoryCommands:

        import dumpTools
        import entropyTools
        def __str__(self):
            return self.inst_name

        def __init__(self, HostInterface, Authenticator):

            self.HostInterface = HostInterface
            self.Authenticator = Authenticator
            self.inst_name = "repository-server"
            self.no_acked_commands = []
            self.termination_commands = []
            self.initialization_commands = []
            self.login_pass_commands = []
            self.no_session_commands = []

            self.valid_commands = {
                'dbdiff':    {
                    'auth': False,
                    'built_in': False,
                    'cb': self.docmd_dbdiff,
                    'args': ["myargs"],
                    'as_user': False,
                    'desc': "returns idpackage differences against the latest available repository",
                    'syntax': "<SESSION_ID> dbdiff <repository> <arch> <product> [idpackages]",
                    'from': str(self), # from what class
                },
                'pkginfo':    {
                    'auth': False,
                    'built_in': False,
                    'cb': self.docmd_pkginfo,
                    'args': ["myargs"],
                    'as_user': False,
                    'desc': "returns idpackage differences against the latest available repository",
                    'syntax': "<SESSION_ID> pkginfo <content fmt True/False> <repository> <arch> <product> <idpackage>",
                    'from': str(self), # from what class
                },
            }

        def register(
                self,
                valid_commands,
                no_acked_commands,
                termination_commands,
                initialization_commands,
                login_pass_commads,
                no_session_commands
            ):
            valid_commands.update(self.valid_commands)
            no_acked_commands.extend(self.no_acked_commands)
            termination_commands.extend(self.termination_commands)
            initialization_commands.extend(self.initialization_commands)
            login_pass_commads.extend(self.login_pass_commands)
            no_session_commands.extend(self.no_session_commands)

        def docmd_dbdiff(self, myargs):

            if len(myargs) < 4:
                return None
            repository = myargs[0]
            arch = myargs[1]
            product = myargs[2]
            foreign_idpackages = myargs[3:]
            x = (repository,arch,product,)

            valid = self.HostInterface.is_repository_available(x)
            if not valid:
                return valid

            dbpath = self.get_database_path(repository, arch, product)
            dbconn = self.HostInterface.open_db(dbpath)
            mychecksum = dbconn.database_checksum(do_order = True, strict = False)
            myids = dbconn.listAllIdpackages()
            foreign_idpackages = set(foreign_idpackages)

            removed_ids = foreign_idpackages - myids
            added_ids = myids - foreign_idpackages

            return {'removed': removed_ids, 'added': added_ids, 'checksum': mychecksum}

        def docmd_pkginfo(self, myargs):
            if len(myargs) < 5:
                return None
            format_content_for_insert = myargs[0]
            if type(format_content_for_insert) is not bool:
                format_content_for_insert = False
            repository = myargs[1]
            arch = myargs[2]
            product = myargs[3]
            zidpackages = myargs[4:]
            idpackages = []
            for idpackage in zidpackages:
                if type(idpackage) is int:
                    idpackages.append(idpackage)
            if not idpackages:
                return None
            idpackages = tuple(sorted(idpackages))
            x = (repository,arch,product,)

            valid = self.HostInterface.is_repository_available(x)
            if not valid:
                return valid

            cached = self.HostInterface.get_dcache((repository, arch, product, idpackages, 'docmd_pkginfo'), repository)
            if cached != None:
                return cached

            dbpath = self.get_database_path(repository, arch, product)
            dbconn = self.HostInterface.open_db(dbpath)
            result = {}

            for idpackage in idpackages:
                try:
                    mydata = dbconn.getPackageData(
                        idpackage,
                        content_insert_formatted = format_content_for_insert,
                        trigger_unicode = True
                    )
                except:
                    self.entropyTools.printTraceback()
                    return None
                result[idpackage] = mydata.copy()

            if result:
                self.HostInterface.set_dcache((repository, arch, product, idpackages, 'docmd_pkginfo'), result, repository)

            return result

        def get_database_path(self, repository, arch, product):
            repoitems = (repository,arch,product)
            mydbroot = self.HostInterface.repositories[repoitems]['dbpath']
            dbpath = os.path.join(mydbroot,etpConst['etpdatabasefile'])
            return dbpath

    class ServiceInterface(TextInterface):
        def __init__(self, *args, **kwargs):
            pass

    import entropyTools, dumpTools
    def __init__(self, repositories, do_ssl = False):
        self.Entropy = EquoInterface(noclientdb = 2)
        self.do_ssl = do_ssl
        self.LockScanner = None
        self.syscache = {
            'db': {},
        }
        etpConst['socket_service']['max_connections'] = 5000
        SocketHostInterface.__init__(
            self,
            self.ServiceInterface,
            noclientdb = 2,
            sock_output = self.Entropy,
            ssl = do_ssl,
            external_cmd_classes = [self.RepositoryCommands]
        )
        self.repositories = repositories
        self.expand_repositories()
        # start timed lock file scanning
        self.start_repository_lock_scanner()


    def start_repository_lock_scanner(self):
        self.LockScanner = self.entropyTools.TimeScheduled( self.lock_scan, 0.5 )
        self.LockScanner.setName("Lock_Scanner::"+str(random.random()))
        self.LockScanner.start()

    def set_repository_db_availability(self, repo_tuple):
        self.repositories[repo_tuple]['enabled'] = False
        mydbpath = os.path.join(self.repositories[repo_tuple]['dbpath'],etpConst['etpdatabasefile'])
        if os.path.isfile(mydbpath) and os.access(mydbpath, os.W_OK):
            self.repositories[repo_tuple]['enabled'] = True

    def is_repository_available(self, repo_tuple):

        if repo_tuple not in self.repositories:
            return None
        # is repository being updated
        if self.repositories[repo_tuple]['locked']:
            return False
        # repository database does not exist
        if not self.repositories[repo_tuple]['enabled']:
            return False

        return True

    def lock_scan(self):
        do_lock = set()
        for repository,arch,product in self.repositories:
            x = (repository,arch,product)
            self.set_repository_db_availability(x)
            if not self.repositories[x]['enabled']:
                mytxt = blue("%s.") % (_("database does not exist. Locking services for it"),)
                self.updateProgress(
                    "[%s] %s" % (
                            brown(str(x)),
                            mytxt,
                    ),
                    importance = 1,
                    type = "info"
                )
                do_lock.add(repository)
                continue
            if os.path.isfile(self.repositories[x]['download_lock']) and \
                not self.repositories[x]['locked']:
                    self.repositories[x]['locked'] = True
                    self.close_db(self.repositories[x]['dbpath'])
                    do_lock.add(repository)
                    mytxt = blue("%s.") % (_("database got locked. Locking services for it"),)
                    self.updateProgress(
                        "[%s] %s" % (
                                brown(str(x)),
                                mytxt,
                        ),
                        importance = 1,
                        type = "info"
                    )
            elif not os.path.isfile(self.repositories[x]['download_lock']) and \
                self.repositories[x]['locked']:
                mytxt = blue("%s.") % (_("unlocking and indexing database"),)
                self.updateProgress(
                    "[%s] %s" % (
                            brown(str(x)),
                            mytxt,
                    ),
                    importance = 1,
                    type = "info"
                )
                # woohoo, got unlocked eventually
                mydbpath = os.path.join(self.repositories[x]['dbpath'],etpConst['etpdatabasefile'])
                mydb = self.open_db(mydbpath)
                mydb.createAllIndexes()
                mydb.commitChanges()
                self.Entropy.clear_dump_cache(etpCache['repository_server']+"/"+repository+"/")
                self.repositories[x]['locked'] = False
        for repo in do_lock:
            self.Entropy.clear_dump_cache(etpCache['repository_server']+"/"+repo+"/")

    def get_dcache(self, item, repo = '_norepo_'):
        return self.dumpTools.loadobj(etpCache['repository_server']+"/"+repo+"/"+str(hash(item)))

    def set_dcache(self, item, data, repo = '_norepo_'):
        self.dumpTools.dumpobj(etpCache['repository_server']+"/"+repo+"/"+str(hash(item)),data)

    def close_db(self, dbpath):
        try:
            dbc = self.syscache['db'].pop(dbpath)
            dbc.closeDB()
        except KeyError:
            pass

    def open_db(self, dbpath):
        cached = self.syscache['db'].get(dbpath)
        if cached != None:
            return cached
        dbc = self.Entropy.openGenericDatabase(
            dbpath,
            xcache = False,
            readOnly = True
        )
        self.syscache['db'][dbpath] = dbc
        return dbc

    def expand_repositories(self):

        for repository,arch,product in self.repositories:
            x = (repository,arch,product)
            self.repositories[x]['locked'] = True # loading locked
            self.set_repository_db_availability(x)
            mydbpath = self.repositories[x]['dbpath']
            myrevfile = os.path.join(mydbpath,etpConst['etpdatabaserevisionfile'])
            myrev = '0'
            if os.path.isfile(myrevfile):
                while 1:
                    try:
                        f = open(myrevfile)
                        myrev = f.readline().strip()
                        f.close()
                    except IOError: # should never happen but who knows
                        continue
                    break
            self.repositories[x]['dbrevision'] = myrev
            self.repositories[x]['download_lock'] = os.path.join(
                mydbpath,
                etpConst['etpdatabasedownloadlockfile']
            )

class EntropySocketClientCommands:

    def __init__(self):
        pass

class EntropyRepositorySocketClientCommands(EntropySocketClientCommands):

    import entropyTools, socket, struct
    def __init__(self, EntropyInterface, ServiceInterface):

        if not isinstance(EntropyInterface, (EquoInterface, ServerInterface)) and \
            not issubclass(EntropyInterface, (EquoInterface, ServerInterface)):
                mytxt = _("A valid EquoInterface/ServerInterface based instance is needed")
                raise exceptionTools.IncorrectParameter("IncorrectParameter: %s, (! %s !)" % (EntropyInterface,mytxt,))

        if not isinstance(ServiceInterface, (RepositorySocketClientInterface,)) and \
            not issubclass(ServiceInterface, (RepositorySocketClientInterface,)):
                mytxt = _("A valid RepositorySocketClientInterface based instance is needed")
                raise exceptionTools.IncorrectParameter("IncorrectParameter: %s, (! %s !)" % (ServiceInterface,mytxt,))

        self.Entropy = EntropyInterface
        self.Service = ServiceInterface
        EntropySocketClientCommands.__init__(self)

    def hello(self):
        self.Entropy.updateProgress(
            "%s" % (_("hello world!"),),
            importance = 1,
            type = "info"
        )

    def handle_standard_answer(self, data, repository = None, arch = None, product = None):
        do_skip = False
        # elaborate answer
        if data == None:
            mytxt = _("feature not supported remotely")
            self.Entropy.updateProgress(
                "[%s:%s|%s:%s|%s:%s] %s" % (
                        darkblue(_("repo")),
                        bold(repository),
                        darkred(_("arch")),
                        bold(arch),
                        darkgreen(_("product")),
                        bold(product),
                        blue(mytxt),
                ),
                importance = 1,
                type = "error"
            )
            do_skip = True
        elif not data:
            mytxt = _("service temporarily not available")
            self.Entropy.updateProgress(
                "[%s:%s|%s:%s|%s:%s] %s" % (
                        darkblue(_("repo")),
                        bold(repository),
                        darkred(_("arch")),
                        bold(arch),
                        darkgreen(_("product")),
                        bold(product),
                        blue(mytxt),
                ),
                importance = 1,
                type = "error"
            )
            do_skip = True
        elif data != self.Service.answers['ok']:
            mytxt = _("received wrong answer")
            self.Entropy.updateProgress(
                "[%s:%s|%s:%s|%s:%s] %s: %s" % (
                        darkblue(_("repo")),
                        bold(repository),
                        darkred(_("arch")),
                        bold(arch),
                        darkgreen(_("product")),
                        bold(product),
                        blue(mytxt),
                        repr(data),
                ),
                importance = 1,
                type = "error"
            )
            do_skip = True
        return do_skip

    def get_result(self, session):
        # get the information
        cmd = "%s rc" % (session,)
        self.Service.transmit(cmd)
        try:
            data = self.Service.receive()
            return data
        except:
            self.entropyTools.printTraceback()
            return None

    def convert_stream_to_object(self, data, gzipped, repository = None, arch = None, product = None):

        # unstream object
        try:
            data = self.Service.stream_to_object(data, gzipped)
        except EOFError:
            mytxt = _("cannot convert stream into object")
            self.Entropy.updateProgress(
                "[%s:%s|%s:%s|%s:%s] %s" % (
                        darkblue(_("repo")),
                        bold(repository),
                        darkred(_("arch")),
                        bold(arch),
                        darkgreen(_("product")),
                        bold(product),
                        blue(mytxt),
                ),
                importance = 1,
                type = "error"
            )
            data = None
        return data

    def differential_packages_comparison(self, idpackages, repository, arch, product, session_id = None, compression = True):
        self.Service.check_socket_connection()
        close_session = False
        if session_id == None:
            close_session = True
            session_id = self.Service.open_session()
        if compression:
            docomp = self.set_gzip_compression_on_rc(session_id, True)
        else:
            docomp = False

        myidlist = ' '.join([str(x) for x in idpackages])
        cmd = "%s %s %s %s %s %s" % (session_id, 'dbdiff', repository, arch, product, myidlist,)
        # send command
        self.Service.transmit(cmd)
        # receive answer
        data = self.Service.receive()

        skip = self.handle_standard_answer(data, repository, arch, product)
        if skip:
            if close_session:
                self.Service.close_session(session_id)
            return None

        data = self.get_result(session_id)
        if data == None:
            if close_session:
                self.Service.close_session(session_id)
            return None
        elif not data:
            if close_session:
                self.Service.close_session(session_id)
            return False

        data = self.convert_stream_to_object(data, docomp, repository, arch, product)

        if docomp:
            self.set_gzip_compression_on_rc(session_id, False)

        if close_session:
            self.Service.close_session(session_id)
        return data

    def package_information_handler(self, idpackages, repository, arch, product, session_id, compression):

        close_session = False
        if session_id == None:
            close_session = True
            session_id = self.Service.open_session()
        # set gzip on rc commands
        if compression:
            docomp = self.set_gzip_compression_on_rc(session_id, True)
        else:
            docomp = False

        cmd = "%s %s %s %s %s %s %s" % (
            session_id,
            'pkginfo',
            True,
            repository,
            arch,
            product,
            ' '.join([str(x) for x in idpackages]),
        )
        # send command
        self.Service.transmit(cmd)
        # receive answer
        data = self.Service.receive()

        skip = self.handle_standard_answer(data, repository, arch, product)
        if skip:
            if close_session:
                self.Service.close_session(session_id)
            return None

        data = self.get_result(session_id)
        if data == None:
            if close_session:
                self.Service.close_session(session_id)
            return None
        elif not data:
            if close_session:
                self.Service.close_session(session_id)
            return False

        if docomp:
            self.set_gzip_compression_on_rc(session_id, False) # reset gzip on rc commands
        data = self.convert_stream_to_object(data, docomp, repository, arch, product)
        if close_session:
            self.Service.close_session(session_id)
        return data

    def get_package_information(self, idpackages, repository, arch, product, session_id = None, compression = True):

        self.Service.check_socket_connection()

        tries = 10
        while 1:
            try:
                data = self.package_information_handler(
                    idpackages,
                    repository,
                    arch,
                    product,
                    session_id = session_id,
                    compression = compression
                )
                return data
            except (self.socket.error,self.struct.error,):
                self.Service.reconnect_socket()
                tries -= 1
                if tries == 0:
                    raise



    def set_gzip_compression_on_rc(self, session, do):
        self.Service.check_socket_connection()
        cmd = "%s %s %s %s" % (session, 'session_config', 'compression', do,)
        self.Service.transmit(cmd)
        data = self.Service.receive()
        if data == self.Service.answers['ok']:
            return True
        return False

class RepositorySocketClientInterface:

    import socket
    import dumpTools
    try:
        import cStringIO as stringio
    except ImportError:
        import StringIO as stringio
    def __init__(self, EntropyInterface, ClientCommandsClass, quiet = False):

        if not isinstance(EntropyInterface, (EquoInterface, ServerInterface)) and \
            not issubclass(EntropyInterface, (EquoInterface, ServerInterface)):
                mytxt = _("A valid EquoInterface/ServerInterface based instance is needed")
                raise exceptionTools.IncorrectParameter("IncorrectParameter: %s, (! %s !)" % (EntropyInterface,mytxt,))

        if not issubclass(ClientCommandsClass, (EntropySocketClientCommands,)):
            mytxt = _("A valid EntropySocketClientCommands based class is needed")
            raise exceptionTools.IncorrectParameter("IncorrectParameter: %s, (! %s !)" % (ClientCommandsClass,mytxt,))

        self.answers = etpConst['socket_service']['answers']
        self.Entropy = EntropyInterface
        self.sock_conn = None
        self.hostname = None
        self.hostport = None
        self.quiet = quiet
        self.CmdInterface = ClientCommandsClass(self.Entropy, self)

    def stream_to_object(self, data, gzipped):

        if gzipped:
            import gzip
            myio = self.stringio.StringIO(data)
            myio.seek(0)
            f = gzip.GzipFile(
                filename = 'gzipped_data',
                mode = 'rb',
                fileobj = myio
            )
            obj = self.dumpTools.unserialize(f)
            f.close()
            myio.close()
        else:
            f = self.stringio.StringIO(data)
            obj = self.dumpTools.unserialize(f)
            f.close()

        return obj

    def append_eos(self, data):
        return str(len(data))+self.answers['eos']+data

    def transmit(self, data):
        self.check_socket_connection()
        self.sock_conn.sendall(self.append_eos(data))

    def close_session(self, session_id):
        self.check_socket_connection()
        self.transmit("%s end" % (session_id,))
        data = self.receive()
        return data

    def open_session(self):
        self.check_socket_connection()
        self.transmit('begin')
        data = self.receive()
        return data

    def receive(self):

        myeos = self.answers['eos']
        data = ''
        mylen = -1

        while 1:

            try:
                data = self.sock_conn.recv(256)

                if mylen == -1:
                    if len(data) < len(myeos):
                        data = ''
                        break
                    mylen = data.split(myeos)[0]
                    data = data[len(mylen)+1:]
                    mylen = int(mylen)
                    mylen -= len(data)

                if len(data) < len(myeos):
                    if not self.quiet:
                        mytxt = _("malformed EOS. receive aborted")
                        self.Entropy.updateProgress(
                            "[%s:%s] %s" % (
                                    brown(self.hostname),
                                    bold(str(self.hostport)),
                                    blue(mytxt),
                            ),
                            importance = 1,
                            type = "warning"
                        )
                    return None

                while mylen > 0:
                    data += self.sock_conn.recv(128)
                    mylen -= 128
                break

            except ValueError, e:
                if not self.quiet:
                    mytxt = _("malformed data. receive aborted")
                    self.Entropy.updateProgress(
                        "[%s:%s] %s: %s" % (
                                brown(self.hostname),
                                bold(str(self.hostport)),
                                blue(mytxt),
                                e,
                        ),
                        importance = 1,
                        type = "warning"
                    )
                return None
            except self.socket.timeout, e:
                if not self.quiet:
                    mytxt = _("connection timed out while receiving data")
                    self.Entropy.updateProgress(
                        "[%s:%s] %s: %s" % (
                                brown(self.hostname),
                                bold(str(self.hostport)),
                                blue(mytxt),
                                e,
                        ),
                        importance = 1,
                        type = "warning"
                    )
                return None

        return data

    def reconnect_socket(self):
        if not self.quiet:
            mytxt = _("Reconnecting to socket")
            self.Entropy.updateProgress(
                "[%s:%s] %s" % (
                        brown(self.hostname),
                        bold(str(self.hostport)),
                        blue(mytxt),
                ),
                importance = 1,
                type = "info"
            )
        self.connect(self.hostname,self.hostport)

    def check_socket_connection(self):
        if not self.sock_conn:
            raise exceptionTools.ConnectionError("ConnectionError: %s" % (_("Not connected to host"),))

    def connect(self, host, port):
        self.sock_conn = self.socket.socket(self.socket.AF_INET, self.socket.SOCK_STREAM)
        try:
            self.sock_conn.connect((host, port))
        except self.socket.error, e:
            if e[0] == 111:
                mytxt = "%s: %s, %s: %s" % (_("Cannot connect to"),host,_("on port"),port,)
                raise exceptionTools.ConnectionError("ConnectionError: %s" % (mytxt,))
            else:
                raise
        self.hostname = host
        self.hostport = port
        if not self.quiet:
            mytxt = _("Successfully connected to host")
            self.Entropy.updateProgress(
                "[%s:%s] %s" % (
                        brown(self.hostname),
                        bold(str(self.hostport)),
                        blue(mytxt),
                ),
                importance = 1,
                type = "info"
            )

    def disconnect(self):
        if not self.sock_conn:
            return True
        self.sock_conn.close()
        if not self.quiet:
            mytxt = _("Successfully disconnected from host")
            self.Entropy.updateProgress(
                "[%s:%s] %s" % (
                        brown(self.hostname),
                        bold(str(self.hostport)),
                        blue(mytxt),
                ),
                importance = 1,
                type = "info"
            )
        self.sock_conn = None
        self.hostname = None
        self.hostport = None


class ServerMirrorsInterface:

    import entropyTools, dumpTools
    def __init__(self,  ServerInstance, repo = None):

        if not isinstance(ServerInstance,ServerInterface):
            mytxt = _("A valid ServerInterface based instance is needed")
            raise exceptionTools.IncorrectParameter("IncorrectParameter: %s" % (mytxt,))

        self.Entropy = ServerInstance
        self.FtpInterface = self.Entropy.FtpInterface
        self.rssFeed = self.Entropy.rssFeed

        mytxt = blue("%s:") % (_("Entropy Server Mirrors Interface loaded"),)
        self.Entropy.updateProgress(
            mytxt,
            importance = 2,
            type = "info",
            header = red(" @@ ")
        )
        mytxt = _("mirror")
        for mirror in self.Entropy.get_remote_mirrors(repo):
            mirror = self.entropyTools.hideFTPpassword(mirror)
            self.Entropy.updateProgress(
                blue("%s: %s") % (mytxt,darkgreen(mirror),),
                importance = 0,
                type = "info",
                header = brown("   # ")
            )


    def lock_mirrors(self, lock = True, mirrors = [], repo = None):

        if repo == None:
            repo = self.Entropy.default_repository

        if not mirrors:
            mirrors = self.Entropy.get_remote_mirrors(repo)

        issues = False
        for uri in mirrors:

            crippled_uri = self.entropyTools.extractFTPHostFromUri(uri)

            lock_text = _("unlocking")
            if lock: lock_text = _("locking")
            self.Entropy.updateProgress(
                "[repo:%s|%s] %s %s" % (
                    brown(repo),
                    darkgreen(crippled_uri),
                    bold(lock_text),
                    blue("%s...") % (_("mirror"),),
                ),
                importance = 1,
                type = "info",
                header = brown(" * "),
                back = True
            )

            ftp = self.FtpInterface(uri, self.Entropy)
            ftp.setCWD(self.Entropy.get_remote_database_relative_path(repo), dodir = True)

            if lock and ftp.isFileAvailable(etpConst['etpdatabaselockfile']):
                self.Entropy.updateProgress(
                    "[repo:%s|%s] %s" % (
                            brown(repo),
                            darkgreen(crippled_uri),
                            blue(_("mirror already locked")),
                    ),
                    importance = 1,
                    type = "info",
                    header = darkgreen(" * ")
                )
                ftp.closeConnection()
                continue
            elif not lock and not ftp.isFileAvailable(etpConst['etpdatabaselockfile']):
                self.Entropy.updateProgress(
                    "[repo:%s|%s] %s" % (
                            brown(repo),
                            darkgreen(crippled_uri),
                            blue(_("mirror already unlocked")),
                    ),
                    importance = 1,
                    type = "info",
                    header = darkgreen(" * ")
                )
                ftp.closeConnection()
                continue

            if lock:
                rc = self.do_mirror_lock(uri, ftp, repo = repo)
            else:
                rc = self.do_mirror_unlock(uri, ftp, repo = repo)
            ftp.closeConnection()
            if not rc: issues = True

        if not issues:
            database_taint_file = self.Entropy.get_local_database_taint_file(repo)
            if os.path.isfile(database_taint_file):
                os.remove(database_taint_file)

        return issues

    # this functions makes entropy clients to not download anything from the chosen
    # mirrors. it is used to avoid clients to download databases while we're uploading
    # a new one.
    def lock_mirrors_for_download(self, lock = True, mirrors = [], repo = None):

        if repo == None:
            repo = self.Entropy.default_repository

        if not mirrors:
            mirrors = self.Entropy.get_remote_mirrors(repo)

        issues = False
        for uri in mirrors:

            crippled_uri = self.entropyTools.extractFTPHostFromUri(uri)

            lock_text = _("unlocking")
            if lock: lock_text = _("locking")
            self.Entropy.updateProgress(
                "[repo:%s|%s] %s %s..." % (
                            blue(repo),
                            red(crippled_uri),
                            bold(lock_text),
                            blue(_("mirror for download")),
                    ),
                importance = 1,
                type = "info",
                header = red(" @@ "),
                back = True
            )

            ftp = self.FtpInterface(uri, self.Entropy)
            ftp.setCWD(self.Entropy.get_remote_database_relative_path(repo), dodir = True)

            if lock and ftp.isFileAvailable(etpConst['etpdatabasedownloadlockfile']):
                self.Entropy.updateProgress(
                    "[repo:%s|%s] %s" % (
                            blue(repo),
                            red(crippled_uri),
                            blue(_("mirror already locked for download")),
                        ),
                    importance = 1,
                    type = "info",
                    header = red(" @@ ")
                )
                ftp.closeConnection()
                continue
            elif not lock and not ftp.isFileAvailable(etpConst['etpdatabasedownloadlockfile']):
                self.Entropy.updateProgress(
                    "[repo:%s|%s] %s" % (
                            blue(repo),
                            red(crippled_uri),
                            blue(_("mirror already unlocked for download")),
                        ),
                    importance = 1,
                    type = "info",
                    header = red(" @@ ")
                )
                ftp.closeConnection()
                continue

            if lock:
                rc = self.do_mirror_lock(uri, ftp, dblock = False, repo = repo)
            else:
                rc = self.do_mirror_unlock(uri, ftp, dblock = False, repo = repo)
            ftp.closeConnection()
            if not rc: issues = True

        return issues

    def do_mirror_lock(self, uri, ftp_connection = None, dblock = True, repo = None):

        if repo == None:
            repo = self.Entropy.default_repository

        if not ftp_connection:
            ftp_connection = self.FtpInterface(uri, self.Entropy)
            ftp_connection.setCWD(self.Entropy.get_remote_database_relative_path(repo), dodir = True)
        else:
            mycwd = ftp_connection.getCWD()
            if mycwd != self.Entropy.get_remote_database_relative_path(repo):
                ftp_connection.setBasedir()
                ftp_connection.setCWD(self.Entropy.get_remote_database_relative_path(repo), dodir = True)

        crippled_uri = self.entropyTools.extractFTPHostFromUri(uri)
        lock_string = ''
        if dblock:
            self.create_local_database_lockfile(repo)
            lock_file = self.get_database_lockfile(repo)
        else:
            lock_string = _('for download') # locking/unlocking mirror1 for download
            self.create_local_database_download_lockfile(repo)
            lock_file = self.get_database_download_lockfile(repo)

        rc = ftp_connection.uploadFile(lock_file, ascii = True)
        if rc:
            self.Entropy.updateProgress(
                "[repo:%s|%s] %s %s" % (
                            blue(repo),
                            red(crippled_uri),
                            blue(_("mirror successfully locked")),
                            blue(lock_string),
                    ),
                importance = 1,
                type = "info",
                header = red(" @@ ")
            )
        else:
            self.Entropy.updateProgress(
                "[repo:%s|%s] %s: %s - %s %s" % (
                            blue(repo),
                            red(crippled_uri),
                            blue("lock error"),
                            rc,
                            blue(_("mirror not locked")),
                            blue(lock_string),
                    ),
                importance = 1,
                type = "error",
                header = darkred(" * ")
            )
            self.remove_local_database_lockfile(repo)

        return rc


    def do_mirror_unlock(self, uri, ftp_connection, dblock = True, repo = None):

        if repo == None:
            repo = self.Entropy.default_repository

        if not ftp_connection:
            ftp_connection = self.FtpInterface(uri, self.Entropy)
            ftp_connection.setCWD(self.Entropy.get_remote_database_relative_path(repo))
        else:
            mycwd = ftp_connection.getCWD()
            if mycwd != self.Entropy.get_remote_database_relative_path(repo):
                ftp_connection.setBasedir()
                ftp_connection.setCWD(self.Entropy.get_remote_database_relative_path(repo))

        crippled_uri = self.entropyTools.extractFTPHostFromUri(uri)

        if dblock:
            dbfile = etpConst['etpdatabaselockfile']
        else:
            dbfile = etpConst['etpdatabasedownloadlockfile']
        rc = ftp_connection.deleteFile(dbfile)
        if rc:
            self.Entropy.updateProgress(
                "[repo:%s|%s] %s" % (
                            blue(repo),
                            red(crippled_uri),
                            blue(_("mirror successfully unlocked")),
                    ),
                importance = 1,
                type = "info",
                header = darkgreen(" * ")
            )
            if dblock:
                self.remove_local_database_lockfile(repo)
            else:
                self.remove_local_database_download_lockfile(repo)
        else:
            self.Entropy.updateProgress(
                "[repo:%s|%s] %s: %s - %s" % (
                            blue(repo),
                            red(crippled_uri),
                            blue(_("unlock error")),
                            rc,
                            blue(_("mirror not unlocked")),
                    ),
                importance = 1,
                type = "error",
                header = darkred(" * ")
            )

        return rc

    def get_database_lockfile(self, repo = None):
        if repo == None:
            repo = self.Entropy.default_repository
        return os.path.join(self.Entropy.get_local_database_dir(repo),etpConst['etpdatabaselockfile'])

    def get_database_download_lockfile(self, repo = None):
        if repo == None:
            repo = self.Entropy.default_repository
        return os.path.join(self.Entropy.get_local_database_dir(repo),etpConst['etpdatabasedownloadlockfile'])

    def create_local_database_download_lockfile(self, repo = None):
        if repo == None:
            repo = self.Entropy.default_repository
        lock_file = self.get_database_download_lockfile(repo)
        f = open(lock_file,"w")
        f.write("download locked")
        f.flush()
        f.close()

    def create_local_database_lockfile(self, repo = None):
        if repo == None:
            repo = self.Entropy.default_repository
        lock_file = self.get_database_lockfile(repo)
        f = open(lock_file,"w")
        f.write("database locked")
        f.flush()
        f.close()

    def remove_local_database_lockfile(self, repo = None):
        if repo == None:
            repo = self.Entropy.default_repository
        lock_file = self.get_database_lockfile(repo)
        if os.path.isfile(lock_file):
            os.remove(lock_file)

    def remove_local_database_download_lockfile(self, repo = None):
        if repo == None:
            repo = self.Entropy.default_repository
        lock_file = self.get_database_download_lockfile(repo)
        if os.path.isfile(lock_file):
            os.remove(lock_file)

    def download_package(self, uri, pkgfile, branch, repo = None):

        if repo == None:
            repo = self.Entropy.default_repository

        crippled_uri = self.entropyTools.extractFTPHostFromUri(uri)

        tries = 0
        while tries < 5:
            tries += 1

            self.Entropy.updateProgress(
                "[repo:%s|%s|#%s] %s: %s" % (
                    brown(repo),
                    darkgreen(crippled_uri),
                    brown(tries),
                    blue(_("connecting to download package")), # connecting to download package xyz
                    darkgreen(pkgfile),
                ),
                importance = 1,
                type = "info",
                header = darkgreen(" * "),
                back = True
            )

            ftp = self.FtpInterface(uri, self.Entropy)
            dirpath = os.path.join(self.Entropy.get_remote_packages_relative_path(repo),branch)
            ftp.setCWD(dirpath)

            self.Entropy.updateProgress(
                "[repo:%s|%s|#%s] %s: %s" % (
                    brown(repo),
                    darkgreen(crippled_uri),
                    brown(tries),
                    blue(_("downloading package")),
                    darkgreen(pkgfile),
                ),
                importance = 1,
                type = "info",
                header = darkgreen(" * ")
            )

            download_path = os.path.join(self.Entropy.get_local_packages_directory(repo),branch)
            rc = ftp.downloadFile(pkgfile,download_path)
            if not rc:
                self.Entropy.updateProgress(
                    "[repo:%s|%s|#%s] %s: %s %s" % (
                        brown(repo),
                        darkgreen(crippled_uri),
                        brown(tries),
                        blue(_("package")),
                        darkgreen(pkgfile),
                        blue(_("does not exist")),
                    ),
                    importance = 1,
                    type = "error",
                    header = darkred(" !!! ")
                )
                ftp.closeConnection()
                return rc

            dbconn = self.Entropy.openServerDatabase(read_only = True, no_upload = True, repo = repo)
            idpackage = dbconn.getIDPackageFromDownload(pkgfile,branch)
            if idpackage == -1:
                self.Entropy.updateProgress(
                    "[repo:%s|%s|#%s] %s: %s %s" % (
                        brown(repo),
                        darkgreen(crippled_uri),
                        brown(tries),
                        blue(_("package")),
                        darkgreen(pkgfile),
                        blue(_("is not listed in the current repository database!!")),
                    ),
                    importance = 1,
                    type = "error",
                    header = darkred(" !!! ")
                )
                ftp.closeConnection()
                return 0

            storedmd5 = dbconn.retrieveDigest(idpackage)
            self.Entropy.updateProgress(
                "[repo:%s|%s|#%s] %s: %s" % (
                    brown(repo),
                    darkgreen(crippled_uri),
                    brown(tries),
                    blue(_("verifying checksum of package")),
                    darkgreen(pkgfile),
                ),
                importance = 1,
                type = "info",
                header = darkgreen(" * "),
                back = True
            )

            pkg_path = os.path.join(download_path,pkgfile)
            md5check = self.entropyTools.compareMd5(pkg_path,storedmd5)
            if md5check:
                self.Entropy.updateProgress(
                    "[repo:%s|%s|#%s] %s: %s %s" % (
                        brown(repo),
                        darkgreen(crippled_uri),
                        brown(tries),
                        blue(_("package")),
                        darkgreen(pkgfile),
                        blue(_("downloaded successfully")),
                    ),
                    importance = 1,
                    type = "info",
                    header = darkgreen(" * ")
                )
                return True
            else:
                self.Entropy.updateProgress(
                    "[repo:%s|%s|#%s] %s: %s %s" % (
                        brown(repo),
                        darkgreen(crippled_uri),
                        brown(tries),
                        blue(_("package")),
                        darkgreen(pkgfile),
                        blue(_("checksum does not match. re-downloading...")),
                    ),
                    importance = 1,
                    type = "warning",
                    header = darkred(" * ")
                )
                if os.path.isfile(pkg_path):
                    os.remove(pkg_path)

            continue

        # if we get here it means the files hasn't been downloaded properly
        self.Entropy.updateProgress(
            "[repo:%s|%s|#%s] %s: %s %s" % (
                brown(repo),
                darkgreen(crippled_uri),
                brown(tries),
                blue(_("package")),
                darkgreen(pkgfile),
                blue(_("seems broken. Consider to re-package it. Giving up!")),
            ),
            importance = 1,
            type = "error",
            header = darkred(" !!! ")
        )
        return False


    def get_remote_databases_status(self, repo = None):

        if repo == None:
            repo = self.Entropy.default_repository

        data = []
        for uri in self.Entropy.get_remote_mirrors(repo):

            ftp = self.FtpInterface(uri, self.Entropy)
            try:
                ftp.setCWD(self.Entropy.get_remote_database_relative_path(repo))
            except ftp.ftplib.error_perm:
                crippled_uri = self.entropyTools.extractFTPHostFromUri(uri)
                self.Entropy.updateProgress(
                    "[repo:%s|%s] %s !" % (
                            brown(repo),
                            darkgreen(crippled_uri),
                            blue(_("mirror doesn't have a valid directory structure")),
                    ),
                    importance = 1,
                    type = "warning",
                    header = darkred(" !!! ")
                )
                ftp.closeConnection()
                continue
            cmethod = etpConst['etpdatabasecompressclasses'].get(etpConst['etpdatabasefileformat'])
            if cmethod == None:
                raise exceptionTools.InvalidDataType("InvalidDataType: %s." % (
                        _("Wrong database compression method passed"),
                    )
                )
            compressedfile = etpConst[cmethod[2]]

            revision = 0
            rc1 = ftp.isFileAvailable(compressedfile)
            revfilename = os.path.basename(self.Entropy.get_local_database_revision_file(repo))
            rc2 = ftp.isFileAvailable(revfilename)
            if rc1 and rc2:
                revision_localtmppath = os.path.join(etpConst['packagestmpdir'],revfilename)
                ftp.downloadFile(revfilename,etpConst['packagestmpdir'],True)
                f = open(revision_localtmppath,"r")
                try:
                    revision = int(f.readline().strip())
                except ValueError:
                    crippled_uri = self.entropyTools.extractFTPHostFromUri(uri)
                    self.Entropy.updateProgress(
                        "[repo:%s|%s] %s: %s" % (
                                brown(repo),
                                darkgreen(crippled_uri),
                                blue(_("mirror doesn't have a valid database revision file")),
                                bold(revision),
                        ),
                        importance = 1,
                        type = "error",
                        header = darkred(" !!! ")
                    )
                    revision = 0
                f.close()
                if os.path.isfile(revision_localtmppath):
                    os.remove(revision_localtmppath)

            info = [uri,revision]
            data.append(info)
            ftp.closeConnection()

        return data

    def is_local_database_locked(self, repo = None):
        x = repo
        if x == None:
            x = self.Entropy.default_repository
        lock_file = self.get_database_lockfile(x)
        return os.path.isfile(lock_file)

    def get_mirrors_lock(self, repo = None):

        dbstatus = []
        for uri in self.Entropy.get_remote_mirrors(repo):
            data = [uri,False,False]
            ftp = FtpInterface(uri, self.Entropy)
            try:
                ftp.setCWD(self.Entropy.get_remote_database_relative_path(repo))
            except ftp.ftplib.error_perm:
                ftp.closeConnection()
                continue
            if ftp.isFileAvailable(etpConst['etpdatabaselockfile']):
                # upload locked
                data[1] = True
            if ftp.isFileAvailable(etpConst['etpdatabasedownloadlockfile']):
                # download locked
                data[2] = True
            ftp.closeConnection()
            dbstatus.append(data)
        return dbstatus

    def update_rss_feed(self, repo = None):

        #db_dir = self.Entropy.get_local_database_dir(repo)
        rss_path = self.Entropy.get_local_database_rss_file(repo)
        rss_light_path = self.Entropy.get_local_database_rsslight_file(repo)
        rss_dump_name = etpConst['rss-dump-name']
        db_revision_path = self.Entropy.get_local_database_revision_file(repo)

        Rss = self.rssFeed(rss_path, maxentries = etpConst['rss-max-entries'])
        # load dump
        db_actions = self.dumpTools.loadobj(rss_dump_name)
        if db_actions:
            try:
                f = open(db_revision_path)
                revision = f.readline().strip()
                f.close()
            except (IOError, OSError):
                revision = "N/A"
            commitmessage = ''
            if etpRSSMessages['commitmessage']:
                commitmessage = ' :: '+etpRSSMessages['commitmessage']
            title = ": "+etpConst['systemname']+" "+etpConst['product'][0].upper()+etpConst['product'][1:]+" "+etpConst['branch']+" :: Revision: "+revision+commitmessage
            link = etpConst['rss-base-url']
            # create description
            added_items = db_actions.get("added")
            if added_items:
                for atom in added_items:
                    mylink = link+"?search="+atom.split("~")[0]+"&arch="+etpConst['currentarch']+"&product="+etpConst['product']
                    description = atom+": "+added_items[atom]['description']
                    Rss.addItem(title = "Added/Updated"+title, link = mylink, description = description)
            removed_items = db_actions.get("removed")
            if removed_items:
                for atom in removed_items:
                    description = atom+": "+removed_items[atom]['description']
                    Rss.addItem(title = "Removed"+title, link = link, description = description)
            light_items = db_actions.get('light')
            if light_items:
                rssLight = self.rssFeed(rss_light_path, maxentries = etpConst['rss-light-max-entries'])
                for atom in light_items:
                    mylink = link+"?search="+atom.split("~")[0]+"&arch="+etpConst['currentarch']+"&product="+etpConst['product']
                    description = light_items[atom]['description']
                    rssLight.addItem(title = "["+revision+"] "+atom, link = mylink, description = description)
                rssLight.writeChanges()

        Rss.writeChanges()
        etpRSSMessages.clear()
        self.dumpTools.removeobj(rss_dump_name)


    # f_out is a file instance
    def dump_database_to_file(self, db_path, destination_path, opener, repo = None):
        f_out = opener(destination_path, "wb")
        dbconn = self.Entropy.openServerDatabase(db_path, just_reading = True, repo = repo)
        dbconn.doDatabaseExport(f_out)
        self.Entropy.close_server_database(dbconn)
        f_out.close()

    def create_file_checksum(self, file_path, checksum_path):
        mydigest = self.entropyTools.md5sum(file_path)
        f = open(checksum_path,"w")
        mystring = "%s  %s\n" % (mydigest,os.path.basename(file_path),)
        f.write(mystring)
        f.flush()
        f.close()

    def compress_file(self, file_path, destination_path, opener):
        f_out = opener(destination_path, "wb")
        f_in = open(file_path,"rb")
        data = f_in.read(8192)
        while data:
            f_out.write(data)
            data = f_in.read(8192)
        f_in.close()
        try:
            f_out.flush()
        except:
            pass
        f_out.close()

    def get_files_to_sync(self, cmethod, download = False, repo = None):

        critical = []
        data = {}
        data['database_revision_file'] = self.Entropy.get_local_database_revision_file(repo)
        critical.append(data['database_revision_file'])
        database_package_mask_file = self.Entropy.get_local_database_mask_file(repo)
        if os.path.isfile(database_package_mask_file) or download:
            data['database_package_mask_file'] = database_package_mask_file
            critical.append(data['database_package_mask_file'])

        database_license_whitelist_file = self.Entropy.get_local_database_licensewhitelist_file(repo)
        if os.path.isfile(database_license_whitelist_file) or download:
            data['database_license_whitelist_file'] = database_license_whitelist_file
            if not download:
                critical.append(data['database_license_whitelist_file'])

        database_rss_file = self.Entropy.get_local_database_rss_file(repo)
        if os.path.isfile(database_rss_file) or download:
            data['database_rss_file'] = database_rss_file
            if not download:
                critical.append(data['database_rss_file'])
        database_rss_light_file = self.Entropy.get_local_database_rsslight_file(repo)
        if os.path.isfile(database_rss_light_file) or download:
            data['database_rss_light_file'] = database_rss_light_file
            if not download:
                critical.append(data['database_rss_light_file'])

        # EAPI 2
        if not download: # we don't need to get the dump
            data['dump_path'] = os.path.join(self.Entropy.get_local_database_dir(repo),etpConst[cmethod[3]])
            critical.append(data['dump_path'])
            data['dump_path_digest'] = os.path.join(self.Entropy.get_local_database_dir(repo),etpConst[cmethod[4]])
            critical.append(data['dump_path_digest'])

        # EAPI 1
        data['compressed_database_path'] = os.path.join(self.Entropy.get_local_database_dir(repo),etpConst[cmethod[2]])
        critical.append(data['compressed_database_path'])
        data['compressed_database_path_digest'] = os.path.join(
            self.Entropy.get_local_database_dir(repo),etpConst['etpdatabasehashfile']
        )
        critical.append(data['compressed_database_path_digest'])

        # Some information regarding how packages are built
        spm_files = [
            (etpConst['spm']['global_make_conf'],"global_make_conf"),
            (etpConst['spm']['global_package_keywords'],"global_package_keywords"),
            (etpConst['spm']['global_package_use'],"global_package_use"),
            (etpConst['spm']['global_package_mask'],"global_package_mask"),
            (etpConst['spm']['global_package_unmask'],"global_package_unmask"),
        ]
        for myfile,myname in spm_files:
            if os.path.isfile(myfile) and os.access(myfile,os.R_OK):
                data[myname] = myfile

        make_profile = etpConst['spm']['global_make_profile']
        if os.path.islink(make_profile):
            mylink = os.readlink(make_profile)
            mytmpdir = os.path.dirname(self.Entropy.entropyTools.getRandomTempFile())
            mytmpfile = os.path.join(mytmpdir,'profile.link')
            f = open(mytmpfile,"w")
            f.write(mylink)
            f.flush()
            f.close()
            data['global_make_profile'] = mytmpfile

        return data, critical

    class FileTransceiver:

        import entropyTools
        def __init__(   self,
                        ftp_interface,
                        entropy_interface,
                        uris,
                        files_to_upload,
                        download = False,
                        remove = False,
                        ftp_basedir = None,
                        local_basedir = None,
                        critical_files = [],
                        use_handlers = False,
                        handlers_data = {},
                        repo = None
            ):

            self.FtpInterface = ftp_interface
            self.Entropy = entropy_interface
            if not isinstance(uris,list):
                raise exceptionTools.InvalidDataType("InvalidDataType: %s" % (_("uris must be a list instance"),))
            if not isinstance(files_to_upload,(list,dict)):
                raise exceptionTools.InvalidDataType("InvalidDataType: %s" % (
                        _("files_to_upload must be a list or dict instance"),
                    )
                )
            self.uris = uris
            if isinstance(files_to_upload,list):
                self.myfiles = files_to_upload[:]
            else:
                self.myfiles = [x for x in files_to_upload]
                self.myfiles.sort()
            self.download = download
            self.remove = remove
            self.repo = repo
            if self.repo == None:
                self.repo = self.Entropy.default_repository
            self.use_handlers = use_handlers
            if self.remove:
                self.download = False
                self.use_handlers = False
            if not ftp_basedir:
                # default to database directory
                self.ftp_basedir = str(self.Entropy.get_remote_database_relative_path(self.repo))
            else:
                self.ftp_basedir = str(ftp_basedir)
            if not local_basedir:
                # default to database directory
                self.local_basedir = os.path.dirname(self.Entropy.get_local_database_file(self.repo))
            else:
                self.local_basedir = str(local_basedir)
            self.critical_files = critical_files
            self.handlers_data = handlers_data.copy()

        def handler_verify_upload(self, local_filepath, uri, ftp_connection, counter, maxcount, action, tries):

            crippled_uri = self.entropyTools.extractFTPHostFromUri(uri)

            self.Entropy.updateProgress(
                "[%s|#%s|(%s/%s)] %s: %s" % (
                    blue(crippled_uri),
                    darkgreen(str(tries)),
                    blue(str(counter)),
                    bold(str(maxcount)),
                    darkgreen(_("verifying upload (if supported)")),
                    blue(os.path.basename(local_filepath)),
                ),
                importance = 0,
                type = "info",
                header = red(" @@ "),
                back = True
            )

            checksum = self.Entropy.get_remote_package_checksum(
                self.repo,
                os.path.basename(local_filepath),
                self.handlers_data['branch']
            )
            if checksum == None:
                self.Entropy.updateProgress(
                    "[%s|#%s|(%s/%s)] %s: %s: %s" % (
                        blue(crippled_uri),
                        darkgreen(str(tries)),
                        blue(str(counter)),
                        bold(str(maxcount)),
                        blue(_("digest verification")),
                        os.path.basename(local_filepath),
                        darkred(_("not supported")),
                    ),
                    importance = 0,
                    type = "info",
                    header = red(" @@ ")
                )
                return True
            elif checksum == False:
                self.Entropy.updateProgress(
                    "[%s|#%s|(%s/%s)] %s: %s: %s" % (
                        blue(crippled_uri),
                        darkgreen(str(tries)),
                        blue(str(counter)),
                        bold(str(maxcount)),
                        blue(_("digest verification")),
                        os.path.basename(local_filepath),
                        bold(_("file not found")),
                    ),
                    importance = 0,
                    type = "warning",
                    header = brown(" @@ ")
                )
                return False
            elif len(checksum) == 32:
                # valid? checking
                ckres = self.entropyTools.compareMd5(local_filepath,checksum)
                if ckres:
                    self.Entropy.updateProgress(
                        "[%s|#%s|(%s/%s)] %s: %s: %s" % (
                            blue(crippled_uri),
                            darkgreen(str(tries)),
                            blue(str(counter)),
                            bold(str(maxcount)),
                            blue(_("digest verification")),
                            os.path.basename(local_filepath),
                            darkgreen(_("so far, so good!")),
                        ),
                        importance = 0,
                        type = "info",
                        header = red(" @@ ")
                    )
                    return True
                else:
                    self.Entropy.updateProgress(
                        "[%s|#%s|(%s/%s)] %s: %s: %s" % (
                            blue(crippled_uri),
                            darkgreen(str(tries)),
                            blue(str(counter)),
                            bold(str(maxcount)),
                            blue(_("digest verification")),
                            os.path.basename(local_filepath),
                            darkred(_("invalid checksum")),
                        ),
                        importance = 0,
                        type = "warning",
                        header = brown(" @@ ")
                    )
                    return False
            else:
                self.Entropy.updateProgress(
                    "[%s|#%s|(%s/%s)] %s: %s: %s" % (
                        blue(crippled_uri),
                        darkgreen(str(tries)),
                        blue(str(counter)),
                        bold(str(maxcount)),
                        blue(_("digest verification")),
                        os.path.basename(local_filepath),
                        darkred(_("unknown data returned")),
                    ),
                    importance = 0,
                    type = "warning",
                    header = brown(" @@ ")
                )
                return False

        def go(self):

            broken_uris = set()
            fine_uris = set()
            errors = False
            action = 'upload'
            if self.download:
                action = 'download'
            elif self.remove:
                action = 'remove'

            for uri in self.uris:

                crippled_uri = self.entropyTools.extractFTPHostFromUri(uri)
                self.Entropy.updateProgress(
                    "[%s|%s] %s..." % (
                        blue(crippled_uri),
                        brown(action),
                        blue(_("connecting to mirror")),
                    ),
                    importance = 0,
                    type = "info",
                    header = blue(" @@ ")
                )
                ftp = self.FtpInterface(uri, self.Entropy)
                self.Entropy.updateProgress(
                    "[%s|%s] %s %s..." % (
                        blue(crippled_uri),
                        brown(action),
                        blue(_("changing directory to")),
                        darkgreen(self.Entropy.get_remote_database_relative_path(self.repo)),
                    ),
                    importance = 0,
                    type = "info",
                    header = blue(" @@ ")
                )
                ftp.setCWD(self.ftp_basedir, dodir = True)

                maxcount = len(self.myfiles)
                counter = 0
                for mypath in self.myfiles:

                    syncer = ftp.uploadFile
                    myargs = [mypath]
                    if self.download:
                        syncer = ftp.downloadFile
                        myargs = [os.path.basename(mypath),self.local_basedir]
                    elif self.remove:
                        syncer = ftp.deleteFile

                    counter += 1
                    tries = 0
                    done = False
                    lastrc = None
                    while tries < 8:
                        tries += 1
                        self.Entropy.updateProgress(
                            "[%s|#%s|(%s/%s)] %s: %s" % (
                                blue(crippled_uri),
                                darkgreen(str(tries)),
                                blue(str(counter)),
                                bold(str(maxcount)),
                                blue(action+"ing"),
                                red(os.path.basename(mypath)),
                            ),
                            importance = 0,
                            type = "info",
                            header = red(" @@ ")
                        )
                        rc = syncer(*myargs)
                        if rc and self.use_handlers and not self.download:
                            rc = self.handler_verify_upload(mypath, uri, ftp, counter, maxcount, action, tries)
                        if rc:
                            self.Entropy.updateProgress(
                                "[%s|#%s|(%s/%s)] %s %s: %s" % (
                                            blue(crippled_uri),
                                            darkgreen(str(tries)),
                                            blue(str(counter)),
                                            bold(str(maxcount)),
                                            blue(action),
                                            _("successful"),
                                            red(os.path.basename(mypath)),
                                ),
                                importance = 0,
                                type = "info",
                                header = darkgreen(" @@ ")
                            )
                            done = True
                            break
                        else:
                            self.Entropy.updateProgress(
                                "[%s|#%s|(%s/%s)] %s %s: %s" % (
                                            blue(crippled_uri),
                                            darkgreen(str(tries)),
                                            blue(str(counter)),
                                            bold(str(maxcount)),
                                            blue(action),
                                            brown(_("failed, retrying")),
                                            red(os.path.basename(mypath)),
                                    ),
                                importance = 0,
                                type = "warning",
                                header = brown(" @@ ")
                            )
                            lastrc = rc
                            continue

                    if not done:

                        self.Entropy.updateProgress(
                            "[%s|(%s/%s)] %s %s: %s - %s: %s" % (
                                    blue(crippled_uri),
                                    blue(str(counter)),
                                    bold(str(maxcount)),
                                    blue(action),
                                    darkred("failed, giving up"),
                                    red(os.path.basename(mypath)),
                                    _("error"),
                                    lastrc,
                            ),
                            importance = 1,
                            type = "error",
                            header = darkred(" !!! ")
                        )

                        if mypath not in self.critical_files:
                            self.Entropy.updateProgress(
                                "[%s|(%s/%s)] %s: %s, %s..." % (
                                    blue(crippled_uri),
                                    blue(str(counter)),
                                    bold(str(maxcount)),
                                    blue(_("not critical")),
                                    os.path.basename(mypath),
                                    blue(_("continuing")),
                                ),
                                importance = 1,
                                type = "warning",
                                header = brown(" @@ ")
                            )
                            continue

                        ftp.closeConnection()
                        errors = True
                        broken_uris.add((uri,lastrc))
                        # next mirror
                        break

                # close connection
                ftp.closeConnection()
                fine_uris.add(uri)

            return errors,fine_uris,broken_uris

    def _show_eapi2_upload_messages(self, crippled_uri, database_path, upload_data, cmethod, repo):

        if repo == None:
            repo = self.Entropy.default_repository

        self.Entropy.updateProgress(
            "[repo:%s|%s|%s:%s] %s" % (
                brown(repo),
                darkgreen(crippled_uri),
                red("EAPI"),
                bold("2"),
                blue(_("creating compressed database dump + checksum")),
            ),
            importance = 0,
            type = "info",
            header = darkgreen(" * ")
        )
        self.Entropy.updateProgress(
            "%s: %s" % (_("database path"),blue(database_path),),
            importance = 0,
            type = "info",
            header = brown("    # ")
        )
        self.Entropy.updateProgress(
            "%s: %s" % (_("dump"),blue(upload_data['dump_path']),),
            importance = 0,
            type = "info",
            header = brown("    # ")
        )
        self.Entropy.updateProgress(
            "%s: %s" % (_("dump checksum"),blue(upload_data['dump_path_digest']),),
            importance = 0,
            type = "info",
            header = brown("    # ")
        )
        self.Entropy.updateProgress(
            "%s: %s" % (_("opener"),blue(cmethod[0]),),
            importance = 0,
            type = "info",
            header = brown("    # ")
        )

    def _show_eapi1_upload_messages(self, crippled_uri, database_path, upload_data, cmethod, repo):

        self.Entropy.updateProgress(
            "[repo:%s|%s|%s:%s] %s" % (
                        brown(repo),
                        darkgreen(crippled_uri),
                        red("EAPI"),
                        bold("1"),
                        blue(_("compressing database + checksum")),
            ),
            importance = 0,
            type = "info",
            header = darkgreen(" * "),
            back = True
        )
        self.Entropy.updateProgress(
            "%s: %s" % (_("database path"),blue(database_path),),
            importance = 0,
            type = "info",
            header = brown("    # ")
        )
        self.Entropy.updateProgress(
            "%s: %s" % (_("compressed database path"),blue(upload_data['compressed_database_path']),),
            importance = 0,
            type = "info",
            header = brown("    # ")
        )
        self.Entropy.updateProgress(
            "%s: %s" % (_("compressed checksum"),blue(upload_data['compressed_database_path_digest']),),
            importance = 0,
            type = "info",
            header = brown("    # ")
        )
        self.Entropy.updateProgress(
            "%s: %s" % (_("opener"),blue(cmethod[0]),),
            importance = 0,
            type = "info",
            header = brown("    # ")
        )

    def create_mirror_directories(self, ftp_connection, path_to_create):
        bdir = ""
        for mydir in path_to_create.split("/"):
            bdir += "/"+mydir
            if not ftp_connection.isFileAvailable(bdir):
                try:
                    ftp_connection.mkdir(bdir)
                except Exception, e:
                    error = str(e)
                    if (error.find("550") == -1) and (error.find("File exist") == -1):
                        mytxt = "%s %s, %s: %s" % (
                            _("cannot create mirror directory"),
                            bdir,
                            _("error"),
                            e,
                        )
                        raise exceptionTools.OnlineMirrorError("OnlineMirrorError:  %s" % (mytxt,))

    def mirror_lock_check(self, uri, repo = None):

        if repo == None:
            repo = self.Entropy.default_repository

        gave_up = False
        crippled_uri = self.entropyTools.extractFTPHostFromUri(uri)
        ftp = self.FtpInterface(uri, self.Entropy)
        ftp.setCWD(self.Entropy.get_remote_database_relative_path(repo), dodir = True)

        lock_file = self.get_database_lockfile(repo)
        if not os.path.isfile(lock_file) and ftp.isFileAvailable(os.path.basename(lock_file)):
            self.Entropy.updateProgress(
                red("[repo:%s|%s|%s] %s, %s" % (
                    repo,
                    crippled_uri,
                    _("locking"),
                    _("mirror already locked"),
                    _("waiting up to 2 minutes before giving up"),
                )
                ),
                importance = 1,
                type = "warning",
                header = brown(" * "),
                back = True
            )
            unlocked = False
            count = 0
            while count < 120:
                count += 1
                time.sleep(1)
                if not ftp.isFileAvailable(os.path.basename(lock_file)):
                    self.Entropy.updateProgress(
                        red("[repo:%s|%s|%s] %s !" % (
                                repo,
                                crippled_uri,
                                _("locking"),
                                _("mirror unlocked"),
                            )
                        ),
                        importance = 1,
                        type = "info",
                        header = darkgreen(" * ")
                    )
                    unlocked = True
                    break
            if not unlocked:
                gave_up = True

        ftp.closeConnection()
        return gave_up

    def shrink_database_and_close(self, repo = None):
        dbconn = self.Entropy.openServerDatabase(read_only = False, no_upload = True, repo = repo, indexing = False)
        dbconn.dropAllIndexes()
        dbconn.vacuum()
        dbconn.commitChanges()
        self.Entropy.close_server_database(dbconn)

    def sync_database_treeupdates(self, repo = None):

        if repo == None:
            repo = self.Entropy.default_repository
        dbconn = self.Entropy.openServerDatabase(read_only = False, no_upload = True, repo = repo)
        # grab treeupdates from other databases and inject
        server_repos = etpConst['server_repositories'].keys()
        all_actions = set()
        for myrepo in server_repos:

            # avoid __default__
            if myrepo == etpConst['clientserverrepoid']:
                continue

            mydbc = self.Entropy.openServerDatabase(just_reading = True, repo = myrepo)
            actions = mydbc.listAllTreeUpdatesActions(no_ids_repos = True)
            for data in actions:
                all_actions.add(data)
            if not actions:
                continue
        backed_up_entries = dbconn.listAllTreeUpdatesActions()
        try:
            # clear first
            dbconn.removeTreeUpdatesActions(repo)
            dbconn.insertTreeUpdatesActions(all_actions,repo)
        except Exception, e:
            self.entropyTools.printTraceback()
            mytxt = "%s, %s: %s. %s" % (
                _("Troubles with treeupdates"),
                _("error"),
                e,
                _("Bumping old data back"),
            )
            self.Entropy.updateProgress(
                mytxt,
                importance = 1,
                type = "warning"
            )
            # restore previous data
            dbconn.bumpTreeUpdatesActions(backed_up_entries)

        dbconn.commitChanges()
        self.Entropy.close_server_database(dbconn)

    def upload_database(self, uris, lock_check = False, pretend = False, repo = None):

        if repo == None:
            repo = self.Entropy.default_repository

        # doing some tests
        import gzip
        myt = type(gzip)
        del myt
        import bz2
        myt = type(bz2)
        del myt

        if etpConst['rss-feed']:
            self.update_rss_feed(repo = repo)

        upload_errors = False
        broken_uris = set()
        fine_uris = set()

        for uri in uris:

            cmethod = etpConst['etpdatabasecompressclasses'].get(etpConst['etpdatabasefileformat'])
            if cmethod == None:
                raise exceptionTools.InvalidDataType("InvalidDataType: %s." % (
                        _("wrong database compression method passed"),
                    )
                )

            crippled_uri = self.entropyTools.extractFTPHostFromUri(uri)
            database_path = self.Entropy.get_local_database_file(repo)
            upload_data, critical = self.get_files_to_sync(cmethod, repo = repo)

            if lock_check:
                given_up = self.mirror_lock_check(uri, repo = repo)
                if given_up:
                    upload_errors = True
                    broken_uris.add(uri)
                    continue

            self.lock_mirrors_for_download(True,[uri], repo = repo)

            self.Entropy.updateProgress(
                "[repo:%s|%s|%s] %s" % (
                    repo,
                    crippled_uri,
                    _("upload"),
                    _("preparing to upload database to mirror"),
                ),
                importance = 1,
                type = "info",
                header = darkgreen(" * ")
            )

            self.sync_database_treeupdates(repo)
            self.Entropy.close_server_databases()

            # backup current database to avoid re-indexing
            old_dbpath = self.Entropy.get_local_database_file(repo)
            backup_dbpath = old_dbpath+".up_backup"
            copy_back = False
            if not pretend:
                try:
                    if os.path.isfile(backup_dbpath):
                        os.remove(backup_dbpath)
                    shutil.copy2(old_dbpath,backup_dbpath)
                    copy_back = True
                except:
                    pass

            self.shrink_database_and_close(repo)

            # EAPI 2
            self._show_eapi2_upload_messages(crippled_uri, database_path, upload_data, cmethod, repo)
            # create compressed dump + checksum
            self.dump_database_to_file(database_path, upload_data['dump_path'], eval(cmethod[0]), repo = repo)
            self.create_file_checksum(upload_data['dump_path'], upload_data['dump_path_digest'])

            # EAPI 1
            self._show_eapi1_upload_messages(crippled_uri, database_path, upload_data, cmethod, repo)
            # compress the database
            self.compress_file(database_path, upload_data['compressed_database_path'], eval(cmethod[0]))
            self.create_file_checksum(database_path, upload_data['compressed_database_path_digest'])

            if not pretend:
                # upload
                uploader = self.FileTransceiver(
                    self.FtpInterface,
                    self.Entropy,
                    [uri],
                    [upload_data[x] for x in upload_data],
                    critical_files = critical,
                    repo = repo
                )
                errors, m_fine_uris, m_broken_uris = uploader.go()
                if errors:
                    my_fine_uris = [self.entropyTools.extractFTPHostFromUri(x) for x in m_fine_uris]
                    my_fine_uris.sort()
                    my_broken_uris = [(self.entropyTools.extractFTPHostFromUri(x[0]),x[1]) for x in m_broken_uris]
                    my_broken_uris.sort()
                    self.Entropy.updateProgress(
                        "[repo:%s|%s|%s] %s" % (
                            repo,
                            crippled_uri,
                            _("errors"),
                            _("failed to upload to mirror, not unlocking and continuing"),
                        ),
                        importance = 0,
                        type = "error",
                        header = darkred(" !!! ")
                    )
                    # get reason
                    reason = my_broken_uris[0][1]
                    self.Entropy.updateProgress(
                        blue("%s: %s" % (_("reason"),reason,)),
                        importance = 0,
                        type = "error",
                        header = blue("    # ")
                    )
                    upload_errors = True
                    broken_uris |= m_broken_uris
                    continue

                # copy db back
                if copy_back and os.path.isfile(backup_dbpath):
                    self.Entropy.close_server_databases()
                    further_backup_dbpath = old_dbpath+".security_backup"
                    if os.path.isfile(further_backup_dbpath):
                        os.remove(further_backup_dbpath)
                    shutil.copy2(old_dbpath,further_backup_dbpath)
                    shutil.move(backup_dbpath,old_dbpath)

            # unlock
            self.lock_mirrors_for_download(False,[uri], repo = repo)
            fine_uris |= m_fine_uris

        if not fine_uris:
            upload_errors = True
        return upload_errors, broken_uris, fine_uris


    def download_database(self, uris, lock_check = False, pretend = False, repo = None):

        if repo == None:
            repo = self.Entropy.default_repository

        # doing some tests
        import gzip
        myt = type(gzip)
        del myt
        import bz2
        myt = type(bz2)
        del myt

        download_errors = False
        broken_uris = set()
        fine_uris = set()

        for uri in uris:

            cmethod = etpConst['etpdatabasecompressclasses'].get(etpConst['etpdatabasefileformat'])
            if cmethod == None:
                raise exceptionTools.InvalidDataType("InvalidDataType: %s." % (
                        _("wrong database compression method passed"),
                    )
                )

            crippled_uri = self.entropyTools.extractFTPHostFromUri(uri)
            database_path = self.Entropy.get_local_database_file(repo)
            database_dir_path = os.path.dirname(self.Entropy.get_local_database_file(repo))
            download_data, critical = self.get_files_to_sync(cmethod, download = True, repo = repo)
            mytmpdir = self.entropyTools.getRandomTempFile()
            os.makedirs(mytmpdir)

            self.Entropy.updateProgress(
                "[repo:%s|%s|%s] %s" % (
                    brown(repo),
                    darkgreen(crippled_uri),
                    red(_("download")),
                    blue(_("preparing to download database from mirror")),
                ),
                importance = 1,
                type = "info",
                header = darkgreen(" * ")
            )
            files_to_sync = download_data.keys()
            files_to_sync.sort()
            for myfile in files_to_sync:
                self.Entropy.updateProgress(
                    blue("%s: %s" % (_("download path"),myfile,)),
                    importance = 0,
                    type = "info",
                    header = brown("    # ")
                )

            if lock_check:
                given_up = self.mirror_lock_check(uri, repo = repo)
                if given_up:
                    download_errors = True
                    broken_uris.add(uri)
                    continue

            # avoid having others messing while we're downloading
            self.lock_mirrors(True,[uri], repo = repo)

            if not pretend:
                # download
                downloader = self.FileTransceiver(self.FtpInterface, self.Entropy, [uri], [download_data[x] for x in download_data], download = True, local_basedir = mytmpdir, critical_files = critical, repo = repo)
                errors, m_fine_uris, m_broken_uris = downloader.go()
                if errors:
                    my_fine_uris = [self.entropyTools.extractFTPHostFromUri(x) for x in m_fine_uris]
                    my_fine_uris.sort()
                    my_broken_uris = [(self.entropyTools.extractFTPHostFromUri(x[0]),x[1]) for x in m_broken_uris]
                    my_broken_uris.sort()
                    self.Entropy.updateProgress(
                        "[repo:%s|%s|%s] %s" % (
                            brown(repo),
                            darkgreen(crippled_uri),
                            red(_("errors")),
                            blue(_("failed to download from mirror")),
                        ),
                        importance = 0,
                        type = "error",
                        header = darkred(" !!! ")
                    )
                    # get reason
                    reason = my_broken_uris[0][1]
                    self.Entropy.updateProgress(
                        blue("%s: %s" % (_("reason"),reason,)),
                        importance = 0,
                        type = "error",
                        header = blue("    # ")
                    )
                    download_errors = True
                    broken_uris |= m_broken_uris
                    self.lock_mirrors(False,[uri], repo = repo)
                    continue

                # all fine then, we need to move data from mytmpdir to database_dir_path

                # EAPI 1
                # unpack database
                compressed_db_filename = os.path.basename(download_data['compressed_database_path'])
                uncompressed_db_filename = os.path.basename(database_path)
                compressed_file = os.path.join(mytmpdir,compressed_db_filename)
                uncompressed_file = os.path.join(mytmpdir,uncompressed_db_filename)
                self.entropyTools.uncompress_file(compressed_file, uncompressed_file, eval(cmethod[0]))
                # now move
                for myfile in os.listdir(mytmpdir):
                    fromfile = os.path.join(mytmpdir,myfile)
                    tofile = os.path.join(database_dir_path,myfile)
                    shutil.move(fromfile,tofile)
                    self.Entropy.ClientService.setup_default_file_perms(tofile)

            if os.path.isdir(mytmpdir):
                shutil.rmtree(mytmpdir)
            if os.path.isdir(mytmpdir):
                os.rmdir(mytmpdir)


            fine_uris.add(uri)
            self.lock_mirrors(False,[uri], repo = repo)

        return download_errors, fine_uris, broken_uris

    def calculate_database_sync_queues(self, repo = None):

        if repo == None:
            repo = self.Entropy.default_repository

        remote_status =  self.get_remote_databases_status(repo)
        local_revision = self.Entropy.get_local_database_revision(repo)
        upload_queue = []
        download_latest = ()

        # all mirrors are empty ? I rule
        if not [x for x in remote_status if x[1]]:
            upload_queue = remote_status[:]
        else:
            highest_remote_revision = max([x[1] for x in remote_status])

            if local_revision < highest_remote_revision:
                for x in remote_status:
                    if x[1] == highest_remote_revision:
                        download_latest = x
                        break

            if download_latest:
                upload_queue = [x for x in remote_status if (x[1] < highest_remote_revision)]
            else:
                upload_queue = [x for x in remote_status if (x[1] < local_revision)]

        return download_latest, upload_queue

    def sync_databases(self, no_upload = False, unlock_mirrors = False, repo = None):

        if repo == None:
            repo = self.Entropy.default_repository

        db_locked = False
        if self.is_local_database_locked(repo):
            db_locked = True

        lock_data = self.get_mirrors_lock(repo)
        mirrors_locked = [x for x in lock_data if x[1]]

        if not mirrors_locked and db_locked:
            mytxt = "%s. %s. %s %s" % (
                _("Mirrors are not locked remotely but the local database is"),
                _("It is a nonsense"),
                _("Please remove the lock file"),
                self.get_database_lockfile(repo),
            )
            raise exceptionTools.OnlineMirrorError("OnlineMirrorError: %s" % (mytxt,))

        if mirrors_locked and not db_locked:
            mytxt = "%s, %s %s" % (
                _("At the moment, mirrors are locked, someone is working on their databases"),
                _("try again later"),
                "...",
            )
            raise exceptionTools.OnlineMirrorError("OnlineMirrorError: %s" % (mytxt,))

        download_latest, upload_queue = self.calculate_database_sync_queues(repo)

        if not download_latest and not upload_queue:
            self.Entropy.updateProgress(
                "[repo:%s|%s] %s" % (
                    brown(repo),
                    red(_("sync")), # something short please
                    blue(_("database already in sync")),
                ),
                importance = 1,
                type = "info",
                header = blue(" @@ ")
            )
            return 0, set(), set()

        if download_latest:
            download_uri = download_latest[0]
            download_errors, fine_uris, broken_uris = self.download_database(download_uri, repo = repo)
            if download_errors:
                self.Entropy.updateProgress(
                    "[repo:%s|%s] %s: %s" % (
                        brown(repo),
                        red(_("sync")),
                        blue(_("database sync failed")),
                        red(_("download issues")),
                    ),
                    importance = 1,
                    type = "error",
                    header = darkred(" !!! ")
                )
                return 1,fine_uris,broken_uris
            # XXX: reload revision settings?

        if upload_queue and not no_upload:

            deps_not_found = self.Entropy.dependencies_test()
            if deps_not_found and not self.Entropy.community_repo:
                self.Entropy.updateProgress(
                    "[repo:%s|%s] %s: %s" % (
                        brown(repo),
                        red(_("sync")),
                        blue(_("database sync forbidden")),
                        red(_("dependencies_test() reported errors")),
                    ),
                    importance = 1,
                    type = "error",
                    header = darkred(" !!! ")
                )
                return 3,set(),set()

            uris = [x[0] for x in upload_queue]
            errors, fine_uris, broken_uris = self.upload_database(uris, repo = repo)
            if errors:
                self.Entropy.updateProgress(
                    "[repo:%s|%s] %s: %s" % (
                        brown(repo),
                        red(_("sync")),
                        blue(_("database sync failed")),
                        red(_("upload issues")),
                    ),
                    importance = 1,
                    type = "error",
                    header = darkred(" !!! ")
                )
                return 2,fine_uris,broken_uris


        self.Entropy.updateProgress(
            "[repo:%s|%s] %s" % (
                brown(repo),
                red(_("sync")),
                blue(_("database sync completed successfully")),
            ),
            importance = 1,
            type = "info",
            header = darkgreen(" * ")
        )

        if unlock_mirrors:
            self.lock_mirrors(False, repo = repo)
        return 0, set(), set()


    def calculate_local_upload_files(self, branch, repo = None):
        upload_files = 0
        upload_packages = set()
        upload_dir = os.path.join(self.Entropy.get_local_upload_directory(repo),branch)

        for package in os.listdir(upload_dir):
            if package.endswith(etpConst['packagesext']) or package.endswith(etpConst['packageshashfileext']):
                upload_packages.add(package)
                if package.endswith(etpConst['packagesext']):
                    upload_files += 1

        return upload_files, upload_packages

    def calculate_local_package_files(self, branch, repo = None):
        local_files = 0
        local_packages = set()
        packages_dir = os.path.join(self.Entropy.get_local_packages_directory(repo),branch)

        if not os.path.isdir(packages_dir):
            os.makedirs(packages_dir)

        for package in os.listdir(packages_dir):
            if package.endswith(etpConst['packagesext']) or package.endswith(etpConst['packageshashfileext']):
                local_packages.add(package)
                if package.endswith(etpConst['packagesext']):
                    local_files += 1

        return local_files, local_packages


    def _show_local_sync_stats(self, upload_files, local_files):
        self.Entropy.updateProgress(
            "%s:" % ( blue(_("Local statistics")),),
            importance = 1,
            type = "info",
            header = red(" @@ ")
        )
        self.Entropy.updateProgress(
            red("%s:\t\t%s %s" % (
                    blue(_("upload directory")),
                    bold(str(upload_files)),
                    red(_("files ready")),
                )
            ),
            importance = 0,
            type = "info",
            header = red(" @@ ")
        )
        self.Entropy.updateProgress(
            red("%s:\t\t%s %s" % (
                    blue(_("packages directory")),
                    bold(str(local_files)),
                    red(_("files ready")),
                )
            ),
            importance = 0,
            type = "info",
            header = red(" @@ ")
        )

    def _show_sync_queues(self, upload, download, removal, copy, metainfo, branch):

        # show stats
        for itemdata in upload:
            package = darkgreen(os.path.basename(itemdata[0]))
            size = blue(self.entropyTools.bytesIntoHuman(itemdata[1]))
            self.Entropy.updateProgress(
                "[branch:%s|%s] %s [%s]" % (
                    brown(branch),
                    blue(_("upload")),
                    darkgreen(package),
                    size,
                ),
                importance = 0,
                type = "info",
                header = red("    # ")
            )
        for itemdata in download:
            package = darkred(os.path.basename(itemdata[0]))
            size = blue(self.entropyTools.bytesIntoHuman(itemdata[1]))
            self.Entropy.updateProgress(
                "[branch:%s|%s] %s [%s]" % (
                    brown(branch),
                    darkred(_("download")),
                    blue(package),
                    size,
                ),
                importance = 0,
                type = "info",
                header = red("    # ")
            )
        for itemdata in copy:
            package = darkblue(os.path.basename(itemdata[0]))
            size = blue(self.entropyTools.bytesIntoHuman(itemdata[1]))
            self.Entropy.updateProgress(
                "[branch:%s|%s] %s [%s]" % (
                    brown(branch),
                    darkgreen(_("copy")),
                    brown(package),
                    size,
                ),
                importance = 0,
                type = "info",
                header = red("    # ")
            )
        for itemdata in removal:
            package = brown(os.path.basename(itemdata[0]))
            size = blue(self.entropyTools.bytesIntoHuman(itemdata[1]))
            self.Entropy.updateProgress(
                "[branch:%s|%s] %s [%s]" % (
                    brown(branch),
                    red(_("remove")),
                    red(package),
                    size,
                ),
                importance = 0,
                type = "info",
                header = red("    # ")
            )

        self.Entropy.updateProgress(
            "%s:\t\t\t%s" % (
                blue(_("Packages to be removed")),
                darkred(str(len(removal))),
            ),
            importance = 0,
            type = "info",
            header = blue(" @@ ")
        )
        self.Entropy.updateProgress(
            "%s:\t\t%s" % (
                darkgreen(_("Packages to be moved locally")),
                darkgreen(str(len(copy))),
            ),
            importance = 0,
            type = "info",
            header = blue(" @@ ")
        )
        self.Entropy.updateProgress(
            "%s:\t\t\t%s" % (
                bold(_("Packages to be uploaded")),
                bold(str(len(upload))),
            ),
            importance = 0,
            type = "info",
            header = blue(" @@ ")
        )

        self.Entropy.updateProgress(
            "%s:\t\t\t%s" % (
                darkred(_("Total removal size")),
                darkred(self.entropyTools.bytesIntoHuman(metainfo['removal'])),
            ),
            importance = 0,
            type = "info",
            header = blue(" @@ ")
        )

        self.Entropy.updateProgress(
            "%s:\t\t\t%s" % (
                blue(_("Total upload size")),
                blue(self.entropyTools.bytesIntoHuman(metainfo['upload'])),
            ),
            importance = 0,
            type = "info",
            header = blue(" @@ ")
        )
        self.Entropy.updateProgress(
            "%s:\t\t\t%s" % (
                brown(_("Total download size")),
                brown(self.entropyTools.bytesIntoHuman(metainfo['download'])),
            ),
            importance = 0,
            type = "info",
            header = blue(" @@ ")
        )


    def calculate_remote_package_files(self, uri, branch, ftp_connection = None, repo = None):

        remote_files = 0
        close_conn = False
        remote_packages_data = {}

        def do_cwd():
            ftp_connection.setCWD(self.Entropy.get_remote_packages_relative_path(repo), dodir = True)
            if not ftp_connection.isFileAvailable(branch):
                ftp_connection.mkdir(branch)
            ftp_connection.setCWD(branch)

        if ftp_connection == None:
            close_conn = True
            ftp_connection = self.FtpInterface(uri, self.Entropy)
        do_cwd()

        remote_packages = ftp_connection.listDir()
        remote_packages_info = ftp_connection.getRoughList()
        if close_conn:
            ftp_connection.closeConnection()

        for tbz2 in remote_packages:
            if tbz2.endswith(etpConst['packagesext']):
                remote_files += 1

        for remote_package in remote_packages_info:
            remote_packages_data[remote_package.split()[8]] = int(remote_package.split()[4])

        return remote_files, remote_packages, remote_packages_data

    def calculate_packages_to_sync(self, uri, branch, repo = None):

        if repo == None:
            repo = self.Entropy.default_repository

        crippled_uri = self.entropyTools.extractFTPHostFromUri(uri)
        upload_files, upload_packages = self.calculate_local_upload_files(branch, repo)
        local_files, local_packages = self.calculate_local_package_files(branch, repo)
        self._show_local_sync_stats(upload_files, local_files)

        self.Entropy.updateProgress(
            "%s: %s" % (blue(_("Remote statistics for")),red(crippled_uri),),
            importance = 1,
            type = "info",
            header = red(" @@ ")
        )
        remote_files, remote_packages, remote_packages_data = self.calculate_remote_package_files(
            uri,
            branch,
            repo = repo
        )
        self.Entropy.updateProgress(
            "%s:\t\t\t%s %s" % (
                blue(_("remote packages")),
                bold(str(remote_files)),
                red(_("files stored")),
            ),
            importance = 0,
            type = "info",
            header = red(" @@ ")
        )

        mytxt = blue("%s ...") % (_("Calculating queues"),)
        self.Entropy.updateProgress(
            mytxt,
            importance = 1,
            type = "info",
            header = red(" @@ ")
        )

        uploadQueue, downloadQueue, removalQueue, fineQueue = self.calculate_sync_queues(
                upload_packages,
                local_packages,
                remote_packages,
                remote_packages_data,
                branch,
                repo
        )
        return uploadQueue, downloadQueue, removalQueue, fineQueue, remote_packages_data

    def calculate_sync_queues(
            self,
            upload_packages,
            local_packages,
            remote_packages,
            remote_packages_data,
            branch,
            repo = None
        ):

        uploadQueue = set()
        downloadQueue = set()
        removalQueue = set()
        fineQueue = set()

        for local_package in upload_packages:
            if local_package in remote_packages:
                local_filepath = os.path.join(self.Entropy.get_local_upload_directory(repo),branch,local_package)
                local_size = int(os.stat(local_filepath)[6])
                remote_size = remote_packages_data.get(local_package)
                if remote_size == None:
                    remote_size = 0
                if (local_size != remote_size):
                    # size does not match, adding to the upload queue
                    uploadQueue.add(local_package)
                else:
                    fineQueue.add(local_package) # just move from upload to packages
            else:
                # always force upload of packages in uploaddir
                uploadQueue.add(local_package)

        # if a package is in the packages directory but not online, we have to upload it
        # we have local_packages and remotePackages
        for local_package in local_packages:
            if local_package in remote_packages:
                local_filepath = os.path.join(self.Entropy.get_local_packages_directory(repo),branch,local_package)
                local_size = int(os.stat(local_filepath)[6])
                remote_size = remote_packages_data.get(local_package)
                if remote_size == None:
                    remote_size = 0
                if (local_size != remote_size) and (local_size != 0):
                    # size does not match, adding to the upload queue
                    if local_package not in fineQueue:
                        uploadQueue.add(local_package)
            else:
                # this means that the local package does not exist
                # so, we need to download it
                uploadQueue.add(local_package)

        # Fill downloadQueue and removalQueue
        for remote_package in remote_packages:
            if remote_package in local_packages:
                local_filepath = os.path.join(self.Entropy.get_local_packages_directory(repo),branch,remote_package)
                local_size = int(os.stat(local_filepath)[6])
                remote_size = remote_packages_data.get(remote_package)
                if remote_size == None:
                    remote_size = 0
                if (local_size != remote_size) and (local_size != 0):
                    # size does not match, remove first
                    if remote_package not in uploadQueue: # do it only if the package has not been added to the uploadQueue
                        removalQueue.add(remote_package) # remotePackage == localPackage # just remove something that differs from the content of the mirror
                        # then add to the download queue
                        downloadQueue.add(remote_package)
            else:
                # this means that the local package does not exist
                # so, we need to download it
                if not remote_package.endswith(".tmp"): # ignore .tmp files
                    downloadQueue.add(remote_package)

        # Collect packages that don't exist anymore in the database
        # so we can filter them out from the download queue
        dbconn = self.Entropy.openServerDatabase(just_reading = True, repo = repo)
        db_files = dbconn.listBranchPackagesTbz2(branch)

        exclude = set()
        for myfile in downloadQueue:
            if myfile.endswith(etpConst['packagesext']):
                if myfile not in db_files:
                    exclude.add(myfile)
        downloadQueue -= exclude

        exclude = set()
        for myfile in uploadQueue:
            if myfile.endswith(etpConst['packagesext']):
                if myfile not in db_files:
                    exclude.add(myfile)
        uploadQueue -= exclude

        exclude = set()
        for myfile in downloadQueue:
            if myfile in uploadQueue:
                exclude.add(myfile)
        downloadQueue -= exclude

        return uploadQueue, downloadQueue, removalQueue, fineQueue


    def expand_queues(self, uploadQueue, downloadQueue, removalQueue, remote_packages_data, branch, repo):

        metainfo = {
            'removal': 0,
            'download': 0,
            'upload': 0,
        }
        removal = []
        download = []
        copy = []
        upload = []

        for item in removalQueue:
            if not item.endswith(etpConst['packagesext']):
                continue
            local_filepath = os.path.join(self.Entropy.get_local_packages_directory(repo),branch,item)
            size = int(os.stat(local_filepath)[6])
            metainfo['removal'] += size
            removal.append((local_filepath,size))

        for item in downloadQueue:
            if not item.endswith(etpConst['packagesext']):
                continue
            local_filepath = os.path.join(self.Entropy.get_local_upload_directory(repo),branch,item)
            if not os.path.isfile(local_filepath):
                size = remote_packages_data.get(item)
                if size == None:
                    size = 0
                size = int(size)
                metainfo['removal'] += size
                download.append((local_filepath,size))
            else:
                size = int(os.stat(local_filepath)[6])
                copy.append((local_filepath,size))

        for item in uploadQueue:
            if not item.endswith(etpConst['packagesext']):
                continue
            local_filepath = os.path.join(self.Entropy.get_local_upload_directory(repo),branch,item)
            local_filepath_pkgs = os.path.join(self.Entropy.get_local_packages_directory(repo),branch,item)
            if os.path.isfile(local_filepath):
                size = int(os.stat(local_filepath)[6])
                upload.append((local_filepath,size))
            else:
                size = int(os.stat(local_filepath_pkgs)[6])
                upload.append((local_filepath_pkgs,size))
            metainfo['upload'] += size


        return upload, download, removal, copy, metainfo


    def _sync_run_removal_queue(self, removal_queue, branch, repo = None):

        if repo == None:
            repo = self.Entropy.default_repository

        for itemdata in removal_queue:

            remove_filename = itemdata[0]
            remove_filepath = os.path.join(self.Entropy.get_local_packages_directory(repo),branch,remove_filename)
            remove_filepath_hash = remove_filepath+etpConst['packageshashfileext']
            self.Entropy.updateProgress(
                "[repo:%s|%s|%s] %s: %s [%s]" % (
                        brown(repo),
                        red("sync"),
                        brown(branch),
                        blue(_("removing package+hash")),
                        darkgreen(remove_filename),
                        blue(self.entropyTools.bytesIntoHuman(itemdata[1])),
                ),
                importance = 0,
                type = "info",
                header = darkred(" * ")
            )

            if os.path.isfile(remove_filepath):
                os.remove(remove_filepath)
            if os.path.isfile(remove_filepath_hash):
                os.remove(remove_filepath_hash)

        self.Entropy.updateProgress(
            "[repo:%s|%s|%s] %s" % (
                    brown(repo),
                    red(_("sync")),
                    brown(branch),
                    blue(_("removal complete")),
            ),
            importance = 0,
            type = "info",
            header = darkred(" * ")
        )


    def _sync_run_copy_queue(self, copy_queue, branch, repo = None):

        if repo == None:
            repo = self.Entropy.default_repository

        for itemdata in copy_queue:

            from_file = itemdata[0]
            from_file_hash = from_file+etpConst['packageshashfileext']
            to_file = os.path.join(self.Entropy.get_local_packages_directory(repo),branch,os.path.basename(from_file))
            to_file_hash = to_file+etpConst['packageshashfileext']
            expiration_file = to_file+etpConst['packagesexpirationfileext']
            self.Entropy.updateProgress(
                "[repo:%s|%s|%s] %s: %s" % (
                        brown(repo),
                        red("sync"),
                        brown(branch),
                        blue(_("copying file+hash to repository")),
                        darkgreen(from_file),
                ),
                importance = 0,
                type = "info",
                header = darkred(" * ")
            )

            if not os.path.isdir(os.path.dirname(to_file)):
                os.makedirs(os.path.dirname(to_file))

            shutil.copy2(from_file,to_file)
            if not os.path.isfile(from_file_hash):
                self.create_file_checksum(from_file, from_file_hash)
            shutil.copy2(from_file_hash,to_file_hash)

            # clear expiration file
            if os.path.isfile(expiration_file):
                os.remove(expiration_file)


    def _sync_run_upload_queue(self, uri, upload_queue, branch, repo = None):

        if repo == None:
            repo = self.Entropy.default_repository

        crippled_uri = self.entropyTools.extractFTPHostFromUri(uri)
        myqueue = []
        for itemdata in upload_queue:
            x = itemdata[0]
            hash_file = x+etpConst['packageshashfileext']
            if not os.path.isfile(hash_file):
                self.entropyTools.createHashFile(x)
            myqueue.append(hash_file)
            myqueue.append(x)

        ftp_basedir = os.path.join(self.Entropy.get_remote_packages_relative_path(repo),branch)
        uploader = self.FileTransceiver(    self.FtpInterface,
                                            self.Entropy,
                                            [uri],
                                            myqueue,
                                            critical_files = myqueue,
                                            use_handlers = True,
                                            ftp_basedir = ftp_basedir,
                                            handlers_data = {'branch': branch },
                                            repo = repo
                                        )
        errors, m_fine_uris, m_broken_uris = uploader.go()
        if errors:
            my_broken_uris = [(self.entropyTools.extractFTPHostFromUri(x[0]),x[1]) for x in m_broken_uris]
            reason = my_broken_uris[0][1]
            self.Entropy.updateProgress(
                "[branch:%s] %s: %s, %s: %s" % (
                            brown(branch),
                            blue(_("upload errors")),
                            red(crippled_uri),
                            blue(_("reason")),
                            darkgreen(str(reason)),
                ),
                importance = 1,
                type = "error",
                header = darkred(" !!! ")
            )
            return errors, m_fine_uris, m_broken_uris

        self.Entropy.updateProgress(
            "[branch:%s] %s: %s" % (
                        brown(branch),
                        blue(_("upload completed successfully")),
                        red(crippled_uri),
            ),
            importance = 1,
            type = "info",
            header = blue(" @@ ")
        )
        return errors, m_fine_uris, m_broken_uris


    def _sync_run_download_queue(self, uri, download_queue, branch, repo = None):

        if repo == None:
            repo = self.Entropy.default_repository

        crippled_uri = self.entropyTools.extractFTPHostFromUri(uri)
        myqueue = []
        for itemdata in download_queue:
            x = itemdata[0]
            hash_file = x+etpConst['packageshashfileext']
            myqueue.append(x)
            myqueue.append(hash_file)

        ftp_basedir = os.path.join(self.Entropy.get_remote_packages_relative_path(repo),branch)
        local_basedir = os.path.join(self.Entropy.get_local_packages_directory(repo),branch)
        downloader = self.FileTransceiver(
            self.FtpInterface,
            self.Entropy,
            [uri],
            myqueue,
            critical_files = myqueue,
            use_handlers = True,
            ftp_basedir = ftp_basedir,
            local_basedir = local_basedir,
            handlers_data = {'branch': branch },
            download = True,
            repo = repo
        )
        errors, m_fine_uris, m_broken_uris = downloader.go()
        if errors:
            my_broken_uris = [(self.entropyTools.extractFTPHostFromUri(x[0]),x[1]) for x in m_broken_uris]
            reason = my_broken_uris[0][1]
            self.Entropy.updateProgress(
                "[repo:%s|%s|%s] %s: %s, %s: %s" % (
                    brown(repo),
                    red(_("sync")),
                    brown(branch),
                    blue(_("download errors")),
                    darkgreen(crippled_uri),
                    blue(_("reason")),
                    reason,
                ),
                importance = 1,
                type = "error",
                header = darkred(" !!! ")
            )
            return errors, m_fine_uris, m_broken_uris

        self.Entropy.updateProgress(
            "[repo:%s|%s|%s] %s: %s" % (
                brown(repo),
                red(_("sync")),
                brown(branch),
                blue(_("download completed successfully")),
                darkgreen(crippled_uri),
            ),
            importance = 1,
            type = "info",
            header = darkgreen(" * ")
        )
        return errors, m_fine_uris, m_broken_uris


    def sync_packages(self, ask = True, pretend = False, packages_check = False, repo = None):

        if repo == None:
            repo = self.Entropy.default_repository

        self.Entropy.updateProgress(
            "[repo:%s|%s] %s" % (
                repo,
                red(_("sync")),
                darkgreen(_("starting packages sync")),
            ),
            importance = 1,
            type = "info",
            header = red(" @@ "),
            back = True
        )

        pkgbranches = etpConst['branches']
        successfull_mirrors = set()
        broken_mirrors = set()
        check_data = ()
        mirrors_tainted = False
        mirror_errors = False
        mirrors_errors = False

        for uri in self.Entropy.get_remote_mirrors(repo):

            crippled_uri = self.entropyTools.extractFTPHostFromUri(uri)
            mirror_errors = False

            for mybranch in pkgbranches:

                self.Entropy.updateProgress(
                    "[repo:%s|%s|branch:%s] %s: %s" % (
                        repo,
                        red(_("sync")),
                        brown(mybranch),
                        blue(_("packages sync")),
                        bold(crippled_uri),
                    ),
                    importance = 1,
                    type = "info",
                    header = red(" @@ ")
                )

                uploadQueue, downloadQueue, removalQueue, fineQueue, remote_packages_data = self.calculate_packages_to_sync(
                    uri,
                    mybranch,
                    repo
                )
                del fineQueue

                if (not uploadQueue) and (not downloadQueue) and (not removalQueue):
                    self.Entropy.updateProgress(
                        "[repo:%s|%s|branch:%s] %s: %s" % (
                            repo,
                            red(_("sync")),
                            mybranch,
                            darkgreen(_("nothing to do on")),
                            crippled_uri,
                        ),
                        importance = 1,
                        type = "info",
                        header = darkgreen(" * ")
                    )
                    if pkgbranches[-1] == mybranch:
                        successfull_mirrors.add(uri)
                    continue

                self.Entropy.updateProgress(
                    "%s:" % (blue(_("Expanding queues")),),
                    importance = 1,
                    type = "info",
                    header = red(" ** ")
                )

                upload, download, removal, copy, metainfo = self.expand_queues(
                            uploadQueue,
                            downloadQueue,
                            removalQueue,
                            remote_packages_data,
                            mybranch,
                            repo
                )
                del uploadQueue, downloadQueue, removalQueue, remote_packages_data
                self._show_sync_queues(upload, download, removal, copy, metainfo, mybranch)

                if not len(upload)+len(download)+len(removal)+len(copy):

                    self.Entropy.updateProgress(
                        "[repo:%s|%s|branch:%s] %s %s" % (
                            self.Entropy.default_repository,
                            red(_("sync")),
                            mybranch,
                            blue(_("nothing to sync for")),
                            crippled_uri,
                        ),
                        importance = 1,
                        type = "info",
                        header = darkgreen(" @@ ")
                    )

                    if pkgbranches[-1] == mybranch:
                        successfull_mirrors.add(uri)
                    continue

                if pretend:
                    if pkgbranches[-1] == mybranch:
                        successfull_mirrors.add(uri)
                    continue

                if ask:
                    rc = self.Entropy.askQuestion(_("Would you like to run the steps above ?"))
                    if rc == "No":
                        continue

                try:

                    if removal:
                        self._sync_run_removal_queue(removal, mybranch, repo)
                    if copy:
                        self._sync_run_copy_queue(copy, mybranch, repo)
                    if upload or download:
                        mirrors_tainted = True
                    if upload:
                        d_errors, m_fine_uris, m_broken_uris = self._sync_run_upload_queue(uri, upload, mybranch, repo)
                        if d_errors: mirror_errors = True
                    if download:
                        d_errors, m_fine_uris, m_broken_uris = self._sync_run_download_queue(uri, download, mybranch, repo)
                        if d_errors: mirror_errors = True
                    if (pkgbranches[-1] == mybranch) and not mirror_errors:
                        successfull_mirrors.add(uri)
                    if mirror_errors:
                        mirrors_errors = True

                except KeyboardInterrupt:
                    self.Entropy.updateProgress(
                        "[repo:%s|%s|branch:%s] %s" % (
                            repo,
                            red(_("sync")),
                            mybranch,
                            darkgreen(_("keyboard interrupt !")),
                        ),
                        importance = 1,
                        type = "info",
                        header = darkgreen(" * ")
                    )
                    return mirrors_tainted, mirrors_errors, successfull_mirrors, broken_mirrors, check_data

                except Exception, e:
                    self.entropyTools.printTraceback()
                    mirrors_errors = True
                    broken_mirrors.add(uri)
                    self.Entropy.updateProgress(
                        "[repo:%s|%s|branch:%s] %s: %s, %s: %s" % (
                            repo,
                            red(_("sync")),
                            mybranch,
                            darkred(_("exception caught")),
                            Exception,
                            _("error"),
                            e,
                        ),
                        importance = 1,
                        type = "error",
                        header = darkred(" !!! ")
                    )

                    exc_txt = self.Entropy.entropyTools.printException(returndata = True)
                    for line in exc_txt:
                        self.Entropy.updateProgress(
                            str(line),
                            importance = 1,
                            type = "error",
                            header = darkred(":  ")
                        )

                    if len(successfull_mirrors) > 0:
                        self.Entropy.updateProgress(
                            "[repo:%s|%s|branch:%s] %s" % (
                                repo,
                                red(_("sync")),
                                mybranch,
                                darkred(_("at least one mirror has been sync'd properly, hooray!")),
                            ),
                            importance = 1,
                            type = "error",
                            header = darkred(" !!! ")
                        )
                    continue

        # if at least one server has been synced successfully, move files
        if (len(successfull_mirrors) > 0) and not pretend:
            for branch in pkgbranches:
                branch_dir = os.path.join(self.Entropy.get_local_upload_directory(repo),branch)
                branchcontent = os.listdir(branch_dir)
                for xfile in branchcontent:
                    source = os.path.join(self.Entropy.get_local_upload_directory(repo),branch,xfile)
                    destdir = os.path.join(self.Entropy.get_local_packages_directory(repo),branch)
                    if not os.path.isdir(destdir):
                        os.makedirs(destdir)
                    dest = os.path.join(destdir,xfile)
                    shutil.move(source,dest)
                    # clear expiration file
                    dest_expiration = dest+etpConst['packagesexpirationfileext']
                    if os.path.isfile(dest_expiration):
                        os.remove(dest_expiration)

        if packages_check:
            check_data = self.Entropy.verify_local_packages([], ask = ask, repo = repo)

        return mirrors_tainted, mirrors_errors, successfull_mirrors, broken_mirrors, check_data


    def is_package_expired(self, package_file, branch, repo = None):
        pkg_path = os.path.join(self.Entropy.get_local_packages_directory(repo),branch,package_file)
        pkg_path += etpConst['packagesexpirationfileext']
        if not os.path.isfile(pkg_path):
            return False
        mtime = self.entropyTools.getFileUnixMtime(pkg_path)
        delta = int(etpConst['packagesexpirationdays'])*24*3600
        currmtime = time.time()
        file_delta = currmtime - mtime
        if file_delta > delta:
            return True
        return False

    def create_expiration_file(self, package_file, branch, repo = None, gentle = False):
        pkg_path = os.path.join(self.Entropy.get_local_packages_directory(repo),branch,package_file)
        pkg_path += etpConst['packagesexpirationfileext']
        if gentle and os.path.isfile(pkg_path):
            return
        f = open(pkg_path,"w")
        f.flush()
        f.close()


    def collect_expiring_packages(self, branch, repo = None):
        dbconn = self.Entropy.openServerDatabase(just_reading = True, repo = repo)
        database_bins = set(dbconn.listBranchPackagesTbz2(branch, do_sort = False))
        bins_dir = os.path.join(self.Entropy.get_local_packages_directory(repo),branch)
        repo_bins = []
        if os.path.isdir(bins_dir):
            repo_bins = os.listdir(bins_dir)
        repo_bins = set([x for x in repo_bins if x.endswith(etpConst['packagesext'])])
        repo_bins -= database_bins
        return repo_bins



    def tidy_mirrors(self, ask = True, pretend = False, repo = None):

        if repo == None:
            repo = self.Entropy.default_repository

        pkgbranches = etpConst['branches']
        self.Entropy.updateProgress(
            "[repo:%s|%s|branches:%s] %s" % (
                brown(repo),
                red(_("tidy")),
                blue(str(','.join(pkgbranches))),
                blue(_("collecting expired packages")),
            ),
            importance = 1,
            type = "info",
            header = red(" @@ ")
        )

        branch_data = {}
        errors = False

        for mybranch in pkgbranches:

            branch_data[mybranch] = {}
            branch_data[mybranch]['errors'] = False

            self.Entropy.updateProgress(
                "[branch:%s] %s" % (
                    brown(mybranch),
                    blue(_("collecting expired packages in the selected branches")),
                ),
                importance = 1,
                type = "info",
                header = blue(" @@ ")
            )

            # collect removed packages
            expiring_packages = self.collect_expiring_packages(mybranch, repo)

            removal = []
            for package in expiring_packages:
                expired = self.is_package_expired(package, mybranch, repo)
                if expired:
                    removal.append(package)
                else:
                    self.create_expiration_file(package, mybranch, repo, gentle = True)

            # fill returning data
            branch_data[mybranch]['removal'] = removal[:]

            if not removal:
                self.Entropy.updateProgress(
                    "[branch:%s] %s" % (
                            brown(mybranch),
                            blue(_("nothing to remove on this branch")),
                    ),
                    importance = 1,
                    type = "info",
                    header = blue(" @@ ")
                )
                continue
            else:
                self.Entropy.updateProgress(
                    "[branch:%s] %s:" % (
                        brown(mybranch),
                        blue(_("these are the expired packages")),
                    ),
                    importance = 1,
                    type = "info",
                    header = blue(" @@ ")
                )
                for package in removal:
                    self.Entropy.updateProgress(
                        "[branch:%s] %s: %s" % (
                                    brown(mybranch),
                                    blue(_("remove")),
                                    darkgreen(package),
                            ),
                        importance = 1,
                        type = "info",
                        header = brown("    # ")
                    )

            if pretend:
                continue

            if ask:
                rc = self.Entropy.askQuestion(_("Would you like to continue ?"))
                if rc == "No":
                    continue

            myqueue = []
            for package in removal:
                myqueue.append(package+etpConst['packageshashfileext'])
                myqueue.append(package)
            ftp_basedir = os.path.join(self.Entropy.get_remote_packages_relative_path(repo),mybranch)
            for uri in self.Entropy.get_remote_mirrors(repo):

                self.Entropy.updateProgress(
                    "[branch:%s] %s..." % (
                        brown(mybranch),
                        blue(_("removing packages remotely")),
                    ),
                    importance = 1,
                    type = "info",
                    header = blue(" @@ ")
                )

                crippled_uri = self.entropyTools.extractFTPHostFromUri(uri)
                destroyer = self.FileTransceiver(
                    self.FtpInterface,
                    self.Entropy,
                    [uri],
                    myqueue,
                    critical_files = [],
                    ftp_basedir = ftp_basedir,
                    remove = True,
                    repo = repo
                )
                errors, m_fine_uris, m_broken_uris = destroyer.go()
                if errors:
                    my_broken_uris = [(self.entropyTools.extractFTPHostFromUri(x[0]),x[1]) for x in m_broken_uris]
                    reason = my_broken_uris[0][1]
                    self.Entropy.updateProgress(
                        "[branch:%s] %s: %s, %s: %s" % (
                            brown(mybranch),
                            blue(_("remove errors")),
                            red(crippled_uri),
                            blue(_("reason")),
                            reason,
                        ),
                        importance = 1,
                        type = "warning",
                        header = brown(" !!! ")
                    )
                    branch_data[mybranch]['errors'] = True
                    errors = True

                self.Entropy.updateProgress(
                    "[branch:%s] %s..." % (
                            brown(mybranch),
                            blue(_("removing packages locally")),
                        ),
                    importance = 1,
                    type = "info",
                    header = blue(" @@ ")
                )

                branch_data[mybranch]['removed'] = set()
                for package in removal:
                    package_path = os.path.join(self.Entropy.get_local_packages_directory(repo),mybranch,package)
                    package_path_hash = package_path+etpConst['packageshashfileext']
                    package_path_expired = package_path+etpConst['packagesexpirationfileext']
                    for myfile in [package_path_hash,package_path,package_path_expired]:
                        if os.path.isfile(myfile):
                            self.Entropy.updateProgress(
                                "[branch:%s] %s: %s" % (
                                            brown(mybranch),
                                            blue(_("removing")),
                                            darkgreen(myfile),
                                    ),
                                importance = 1,
                                type = "info",
                                header = brown(" @@ ")
                            )
                            os.remove(myfile)
                            branch_data[mybranch]['removed'].add(myfile)


        return errors, branch_data


class EntropyDatabaseInterface:

    import entropyTools, dumpTools
    def __init__(
            self,
            readOnly = False,
            noUpload = False,
            dbFile = None,
            clientDatabase = False,
            xcache = False,
            dbname = etpConst['serverdbid'],
            indexing = True,
            OutputInterface = None,
            ServiceInterface = None
        ):

        if OutputInterface == None:
            OutputInterface = TextInterface()

        if dbFile == None:
            raise exceptionTools.IncorrectParameter("IncorrectParameter: %s" % (_("valid database path needed"),) )

        self.dbapi2 = dbapi2
        # setup output interface
        self.OutputInterface = OutputInterface
        self.updateProgress = self.OutputInterface.updateProgress
        self.askQuestion = self.OutputInterface.askQuestion
        # setup service interface
        self.ServiceInterface = ServiceInterface
        self.readOnly = readOnly
        self.noUpload = noUpload
        self.clientDatabase = clientDatabase
        self.xcache = xcache
        self.dbname = dbname
        self.indexing = indexing
        if not self.entropyTools.is_user_in_entropy_group():
            # forcing since we won't have write access to db
            self.indexing = False
        # live systems don't like wasting RAM
        if self.entropyTools.islive():
            self.indexing = False
        self.dbFile = dbFile
        self.dbclosed = True
        self.server_repo = None

        if not self.clientDatabase:
            self.server_repo = self.dbname[len(etpConst['serverdbid']):]
            self.create_dbstatus_data()

        # no caching for non root and server connections
        if (self.dbname.startswith(etpConst['serverdbid'])) or (not self.entropyTools.is_user_in_entropy_group()):
            self.xcache = False
        self.live_cache = {}

        # create connection
        self.connection = self.dbapi2.connect(dbFile,timeout=300.0)
        self.cursor = self.connection.cursor()

        try:
            self.cursor.execute('PRAGMA cache_size = 6000')
            self.cursor.execute('PRAGMA default_cache_size = 6000')
        except:
            pass

        if not self.clientDatabase and not self.readOnly:
            # server side is calling
            # lock mirror remotely and ensure to have latest database revision
            self.doServerDatabaseSyncLock(self.noUpload)

        if os.access(self.dbFile,os.W_OK) and self.doesTableExist('baseinfo') and self.doesTableExist('extrainfo'):
            if self.entropyTools.islive():
                # check where's the file
                if etpConst['systemroot']:
                    self.databaseStructureUpdates()
            else:
                self.databaseStructureUpdates()

        # now we can set this to False
        self.dbclosed = False

    def __del__(self):
        if not self.dbclosed:
            self.closeDB()

    def create_dbstatus_data(self):
        taint_file = self.ServiceInterface.get_local_database_taint_file(self.server_repo)
        if not etpDbStatus.has_key(self.dbFile):
            etpDbStatus[self.dbFile] = {}
            etpDbStatus[self.dbFile]['tainted'] = False
            etpDbStatus[self.dbFile]['bumped'] = False
        if os.path.isfile(taint_file):
            etpDbStatus[self.dbFile]['tainted'] = True
            etpDbStatus[self.dbFile]['bumped'] = True

    def doServerDatabaseSyncLock(self, noUpload):

        # check if the database is locked locally
        # self.server_repo
        lock_file = self.ServiceInterface.MirrorsService.get_database_lockfile(self.server_repo)
        if os.path.isfile(lock_file):
            self.updateProgress(
                red(_("Entropy database is already locked by you :-)")),
                importance = 1,
                type = "info",
                header = red(" * ")
            )
        else:
            # check if the database is locked REMOTELY
            mytxt = "%s ..." % (_("Locking and Syncing Entropy database"),)
            self.updateProgress(
                red(mytxt),
                importance = 1,
                type = "info",
                header = red(" * "),
                back = True
            )
            for uri in self.ServiceInterface.get_remote_mirrors(self.server_repo):
                given_up = self.ServiceInterface.MirrorsService.mirror_lock_check(uri, repo = self.server_repo)
                if given_up:
                    crippled_uri = self.entropyTools.extractFTPHostFromUri(uri)
                    mytxt = "%s:" % (_("Mirrors status table"),)
                    self.updateProgress(
                        darkgreen(mytxt),
                        importance = 1,
                        type = "info",
                        header = brown(" * ")
                    )
                    dbstatus = self.ServiceInterface.MirrorsService.get_mirrors_lock(repo = self.server_repo)
                    for db in dbstatus:
                        db[1] = green(_("Unlocked"))
                        if (db[1]):
                            db[1] = red(_("Locked"))
                        db[2] = green(_("Unlocked"))
                        if (db[2]):
                            db[2] = red(_("Locked"))

                        crippled_uri = self.entropyTools.extractFTPHostFromUri(db[0])
                        self.updateProgress(
                            bold("%s: ") + red("[") + brown("DATABASE: %s") + red("] [") + \
                            brown("DOWNLOAD: %s")+red("]") % (
                                crippled_uri,
                                db[1],
                                db[2],
                            ),
                            importance = 1,
                            type = "info",
                            header = "\t"
                        )

                    raise exceptionTools.OnlineMirrorError("OnlineMirrorError: %s %s" % (
                            _("cannot lock mirror"),
                            crippled_uri,
                        )
                    )

            # if we arrive here, it is because all the mirrors are unlocked
            self.ServiceInterface.MirrorsService.lock_mirrors(True, repo = self.server_repo)
            self.ServiceInterface.MirrorsService.sync_databases(noUpload, repo = self.server_repo)

    def closeDB(self):

        self.dbclosed = True

        # if the class is opened readOnly, close and forget
        if self.readOnly:
            self.cursor.close()
            self.connection.close()
            return

        if self.clientDatabase:
            self.commitChanges()
            self.cursor.close()
            self.connection.close()
            return

        if not etpDbStatus[self.dbFile]['tainted']:
            # we can unlock it, no changes were made
            self.ServiceInterface.MirrorsService.lock_mirrors(False, repo = self.server_repo)
        else:
            self.updateProgress(
                darkgreen(_("Mirrors have not been unlocked. Remember to sync them.")),
                importance = 1,
                type = "info",
                header = brown(" * ")
            )

        self.commitChanges()
        #self.vacuum()
        self.cursor.close()
        self.connection.close()

    def vacuum(self):
        self.cursor.execute("vacuum")

    def commitChanges(self):

        if self.readOnly:
            return

        try:
            self.connection.commit()
        except:
            pass

        if not self.clientDatabase:
            self.taintDatabase()
            if (etpDbStatus[self.dbFile]['tainted']) and \
                (not etpDbStatus[self.dbFile]['bumped']):
                    # bump revision, setting DatabaseBump causes the session to just bump once
                    etpDbStatus[self.dbFile]['bumped'] = True
                    self.revisionBump()

    def taintDatabase(self):
        # if it's equo to open it, this should be avoided
        if self.clientDatabase:
            return
        # taint the database status
        taint_file = self.ServiceInterface.get_local_database_taint_file(repo = self.server_repo)
        f = open(taint_file,"w")
        f.write(etpConst['currentarch']+" database tainted\n")
        f.flush()
        f.close()
        etpDbStatus[self.dbFile]['tainted'] = True

    def untaintDatabase(self):
        if (self.clientDatabase): # if it's equo to open it, this should be avoided
            return
        etpDbStatus[self.dbFile]['tainted'] = False
        # untaint the database status
        taint_file = self.ServiceInterface.get_local_database_taint_file(repo = self.server_repo)
        if os.path.isfile(taint_file):
            os.remove(taint_file)

    def revisionBump(self):
        revision_file = self.ServiceInterface.get_local_database_revision_file(repo = self.server_repo)
        if not os.path.isfile(revision_file):
            revision = 1
        else:
            f = open(revision_file,"r")
            revision = int(f.readline().strip())
            revision += 1
            f.close()
        f = open(revision_file,"w")
        f.write(str(revision)+"\n")
        f.flush()
        f.close()

    def isDatabaseTainted(self):
        taint_file = self.ServiceInterface.get_local_database_taint_file(repo = self.server_repo)
        if os.path.isfile(taint_file):
            return True
        return False

    # never use this unless you know what you're doing
    def initializeDatabase(self):
        self.checkReadOnly()
        self.cursor.executescript(etpConst['sql_destroy'])
        self.cursor.executescript(etpConst['sql_init'])
        self.databaseStructureUpdates()
        self.commitChanges()

    def checkReadOnly(self):
        if (self.readOnly):
            raise exceptionTools.OperationNotPermitted("OperationNotPermitted: %s." % (
                    _("can't do that on a readonly database"),
                )
            )

    # check for /usr/portage/profiles/updates changes
    def serverUpdatePackagesData(self):

        etpConst['server_treeupdatescalled'].add(self.server_repo)

        repo_updates_file = self.ServiceInterface.get_local_database_treeupdates_file(self.server_repo)
        doRescan = False

        stored_digest = self.retrieveRepositoryUpdatesDigest(self.server_repo)
        if stored_digest == -1:
            doRescan = True

        # check portage files for changes if doRescan is still false
        portage_dirs_digest = "0"
        if not doRescan:

            if repositoryUpdatesDigestCache_disk.has_key(self.server_repo):
                portage_dirs_digest = repositoryUpdatesDigestCache_disk.get(self.server_repo)
            else:
                SpmIntf = SpmInterface(self.OutputInterface)
                Spm = SpmIntf.intf
                # grab portdir
                updates_dir = etpConst['systemroot']+Spm.get_spm_setting("PORTDIR")+"/profiles/updates"
                if os.path.isdir(updates_dir):
                    # get checksum
                    mdigest = self.entropyTools.md5sum_directory(updates_dir, get_obj = True)
                    # also checksum etpConst['etpdatabaseupdatefile']
                    if os.path.isfile(repo_updates_file):
                        f = open(repo_updates_file)
                        block = f.read(1024)
                        while block:
                            mdigest.update(block)
                            block = f.read(1024)
                        f.close()
                    portage_dirs_digest = mdigest.hexdigest()
                    repositoryUpdatesDigestCache_disk[self.server_repo] = portage_dirs_digest
                del updates_dir

        if doRescan or (str(stored_digest) != str(portage_dirs_digest)):

            # force parameters
            self.readOnly = False
            self.noUpload = True

            # reset database tables
            self.clearTreeupdatesEntries(self.server_repo)

            SpmIntf = SpmInterface(self.OutputInterface)
            Spm = SpmIntf.intf
            updates_dir = etpConst['systemroot']+Spm.get_spm_setting("PORTDIR")+"/profiles/updates"
            update_files = self.entropyTools.sortUpdateFiles(os.listdir(updates_dir))
            update_files = [os.path.join(updates_dir,x) for x in update_files]
            # now load actions from files
            update_actions = []
            for update_file in update_files:
                f = open(update_file,"r")
                mycontent = f.readlines()
                f.close()
                lines = [x.strip() for x in mycontent if x.strip()]
                update_actions.extend(lines)

            # add entropy packages.db.repo_updates content
            if os.path.isfile(repo_updates_file):
                f = open(repo_updates_file,"r")
                mycontent = f.readlines()
                f.close()
                lines = [x.strip() for x in mycontent if x.strip() and not x.strip().startswith("#")]
                update_actions.extend(lines)
            # now filter the required actions
            update_actions = self.filterTreeUpdatesActions(update_actions)
            if update_actions:

                mytxt = "%s: %s. %s %s" % (
                    bold(_("ATTENTION")),
                    red(_("forcing package updates")),
                    red(_("Syncing with")),
                    blue(updates_dir),
                )
                self.updateProgress(
                    mytxt,
                    importance = 1,
                    type = "info",
                    header = brown(" * ")
                )
                # lock database
                self.doServerDatabaseSyncLock(self.noUpload)
                # now run queue
                try:
                    self.runTreeUpdatesActions(update_actions)
                except:
                    # destroy digest
                    self.setRepositoryUpdatesDigest(self.server_repo, "-1")
                    raise

                # store new actions
                self.addRepositoryUpdatesActions(self.server_repo,update_actions)

            # store new digest into database
            self.setRepositoryUpdatesDigest(self.server_repo, portage_dirs_digest)

    # client side, no portage dependency
    # lxnay: it is indeed very similar to serverUpdatePackagesData() but I prefer keeping both separate
    # also, we reuse the same caching dictionaries of the server function
    # repositoryUpdatesDigestCache_disk -> client database cache
    # check for repository packages updates
    # this will read database treeupdates* tables and do
    # changes required if running as root.
    def clientUpdatePackagesData(self, clientDbconn, force = False):

        if clientDbconn == None:
            return

        repository = self.dbname[len(etpConst['dbnamerepoprefix']):]
        etpConst['client_treeupdatescalled'].add(repository)

        doRescan = False
        shell_rescan = os.getenv("ETP_TREEUPDATES_RESCAN")
        if shell_rescan: doRescan = True

        # check database digest
        stored_digest = self.retrieveRepositoryUpdatesDigest(repository)
        if stored_digest == -1:
            doRescan = True

        # check stored value in client database
        client_digest = "0"
        if not doRescan:
            client_digest = clientDbconn.retrieveRepositoryUpdatesDigest(repository)

        if doRescan or (str(stored_digest) != str(client_digest)) or force:

            # reset database tables
            clientDbconn.clearTreeupdatesEntries(repository)

            # load updates
            update_actions = self.retrieveTreeUpdatesActions(repository)
            # now filter the required actions
            update_actions = clientDbconn.filterTreeUpdatesActions(update_actions)

            if update_actions:

                mytxt = "%s: %s. %s %s" % (
                    bold(_("ATTENTION")),
                    red(_("forcing packages metadata update")),
                    red(_("Updating system database using repository id")),
                    blue(repository),
                )
                self.updateProgress(
                    mytxt,
                    importance = 1,
                    type = "info",
                    header = darkred(" * ")
                    )
                # run stuff
                clientDbconn.runTreeUpdatesActions(update_actions)

            # store new digest into database
            clientDbconn.setRepositoryUpdatesDigest(repository, stored_digest)

            # store new actions
            clientDbconn.addRepositoryUpdatesActions(etpConst['clientdbid'],update_actions)

            # clear client cache
            clientDbconn.clearCache()

    # this functions will filter either data from /usr/portage/profiles/updates/*
    # or repository database returning only the needed actions
    def filterTreeUpdatesActions(self, actions):
        new_actions = []
        for action in actions:

            if action in new_actions: # skip dupies
                continue

            doaction = action.split()
            if doaction[0] == "slotmove":
                # slot move
                atom = doaction[1]
                from_slot = doaction[2]
                to_slot = doaction[3]
                category = atom.split("/")[0]
                matches = self.atomMatch(atom, multiMatch = True)
                found = False
                if matches[1] == 0:
                    # found atom, check slot and category
                    for idpackage in matches[0]:
                        myslot = str(self.retrieveSlot(idpackage))
                        mycategory = self.retrieveCategory(idpackage)
                        if mycategory == category:
                            if (myslot == from_slot) and (myslot != to_slot) and (action not in new_actions):
                                new_actions.append(action)
                                found = True
                                break
                    if found:
                        continue
                # if we get here it means found == False
                # search into dependencies
                atom_key = self.entropyTools.dep_getkey(atom)
                dep_atoms = self.searchDependency(atom_key, like = True, multi = True, strings = True)
                dep_atoms = [x for x in dep_atoms if x.endswith(":"+from_slot) and self.entropyTools.dep_getkey(x) == atom_key]
                if dep_atoms:
                    new_actions.append(action)
            elif doaction[0] == "move":
                atom = doaction[1] # usually a key
                category = atom.split("/")[0]
                matches = self.atomMatch(atom, multiMatch = True)
                found = False
                if matches[1] == 0:
                    for idpackage in matches[0]:
                        mycategory = self.retrieveCategory(idpackage)
                        if (mycategory == category) and (action not in new_actions):
                            new_actions.append(action)
                            found = True
                            break
                    if found:
                        continue
                # if we get here it means found == False
                # search into dependencies
                atom_key = self.entropyTools.dep_getkey(atom)
                dep_atoms = self.searchDependency(atom_key, like = True, multi = True, strings = True)
                dep_atoms = [x for x in dep_atoms if self.entropyTools.dep_getkey(x) == atom_key]
                if dep_atoms:
                    new_actions.append(action)
        return new_actions

    # this is the place to add extra actions support
    def runTreeUpdatesActions(self, actions):

        # just run fixpackages if gentoo-compat is enabled
        if etpConst['gentoo-compat']:

            mytxt = "%s: %s, %s." % (
                bold(_("SPM")),
                blue(_("Running fixpackages")),
                red(_("it could take a while")),
            )
            self.updateProgress(
                mytxt,
                importance = 1,
                type = "warning",
                header = darkred(" * ")
            )
            if self.clientDatabase:
                try:
                    Spm = self.ServiceInterface.Spm()
                    Spm.run_fixpackages()
                except:
                    pass
            else:
                self.ServiceInterface.SpmService.run_fixpackages()

        quickpkg_atoms = set()
        for action in actions:
            command = action.split()
            mytxt = "%s: %s: %s." % (
                bold(_("ENTROPY")),
                red(_("action")),
                blue(action),
            )
            self.updateProgress(
                mytxt,
                importance = 1,
                type = "warning",
                header = darkred(" * ")
            )
            if command[0] == "move":
                quickpkg_atoms |= self.runTreeUpdatesMoveAction(command[1:], quickpkg_atoms)
            elif command[0] == "slotmove":
                quickpkg_atoms |= self.runTreeUpdatesSlotmoveAction(command[1:], quickpkg_atoms)

        if quickpkg_atoms and not self.clientDatabase:
            # quickpkg package and packages owning it as a dependency
            try:
                self.runTreeUpdatesQuickpkgAction(quickpkg_atoms)
            except:
                self.entropyTools.printTraceback()
                mytxt = "%s: %s: %s, %s." % (
                    bold(_("WARNING")),
                    red(_("Cannot complete quickpkg for atoms")),
                    blue(str(list(quickpkg_atoms))),
                    _("do it manually"),
                )
                self.updateProgress(
                    mytxt,
                    importance = 1,
                    type = "warning",
                    header = darkred(" * ")
                )
            self.commitChanges()

        # discard cache
        self.clearCache()


    # -- move action:
    # 1) move package key to the new name: category + name + atom
    # 2) update all the dependencies in dependenciesreference to the new key
    # 3) run fixpackages which will update /var/db/pkg files
    # 4) automatically run quickpkg() to build the new binary and
    #    tainted binaries owning tainted iddependency and taint database
    def runTreeUpdatesMoveAction(self, move_command, quickpkg_queue):

        key_from = move_command[0]
        key_to = move_command[1]
        cat_to = key_to.split("/")[0]
        name_to = key_to.split("/")[1]
        matches = self.atomMatch(key_from, multiMatch = True)
        iddependencies_idpackages = set()

        if matches[1] == 0:

            for idpackage in matches[0]:

                slot = self.retrieveSlot(idpackage)
                old_atom = self.retrieveAtom(idpackage)
                new_atom = old_atom.replace(key_from,key_to)

                ### UPDATE DATABASE
                # update category
                self.setCategory(idpackage, cat_to)
                # update name
                self.setName(idpackage, name_to)
                # update atom
                self.setAtom(idpackage, new_atom)

                # look for packages we need to quickpkg again
                # note: quickpkg_queue is simply ignored if self.clientDatabase
                quickpkg_queue.add(key_to+":"+str(slot))

                if not self.clientDatabase:

                    # check for injection and warn the developer
                    injected = self.isInjected(idpackage)
                    if injected:
                        mytxt = "%s: %s %s. %s !!! %s." % (
                            bold(_("INJECT")),
                            blue(str(new_atom)),
                            red(_("has been injected")),
                            red(_("You need to quickpkg it manually to update the embedded database")),
                            red(_("Repository database will be updated anyway")),
                        )
                        self.updateProgress(
                            mytxt,
                            importance = 1,
                            type = "warning",
                            header = darkred(" * ")
                        )

        iddeps = self.searchDependency(key_from, like = True, multi = True)
        for iddep in iddeps:
            # update string
            mydep = self.retrieveDependencyFromIddependency(iddep)
            mydep_key = self.entropyTools.dep_getkey(mydep)
            if mydep_key != key_from: # avoid changing wrong atoms -> dev-python/qscintilla-python would
                continue              # become x11-libs/qscintilla if we don't do this check
            mydep = mydep.replace(key_from,key_to)
            # now update
            # dependstable on server is always re-generated
            self.setDependency(iddep, mydep)
            # we have to repackage also package owning this iddep
            iddependencies_idpackages |= self.searchIdpackageFromIddependency(iddep)

        self.commitChanges()
        quickpkg_queue = list(quickpkg_queue)
        for x in range(len(quickpkg_queue)):
            myatom = quickpkg_queue[x]
            myatom = myatom.replace(key_from,key_to)
            quickpkg_queue[x] = myatom
        quickpkg_queue = set(quickpkg_queue)
        for idpackage_owner in iddependencies_idpackages:
            myatom = self.retrieveAtom(idpackage_owner)
            myatom = myatom.replace(key_from,key_to)
            quickpkg_queue.add(myatom)
        return quickpkg_queue


    # -- slotmove action:
    # 1) move package slot
    # 2) update all the dependencies in dependenciesreference owning same matched atom + slot
    # 3) run fixpackages which will update /var/db/pkg files
    # 4) automatically run quickpkg() to build the new binary and tainted binaries owning tainted iddependency and taint database
    def runTreeUpdatesSlotmoveAction(self, slotmove_command, quickpkg_queue):

        atom = slotmove_command[0]
        atomkey = self.entropyTools.dep_getkey(atom)
        slot_from = slotmove_command[1]
        slot_to = slotmove_command[2]
        matches = self.atomMatch(atom, multiMatch = True)
        iddependencies_idpackages = set()

        if matches[1] == 0:

            for idpackage in matches[0]:

                ### UPDATE DATABASE
                # update slot
                self.setSlot(idpackage, slot_to)

                # look for packages we need to quickpkg again
                # note: quickpkg_queue is simply ignored if self.clientDatabase
                quickpkg_queue.add(atom+":"+str(slot_to))

                if not self.clientDatabase:

                    # check for injection and warn the developer
                    injected = self.isInjected(idpackage)
                    if injected:
                        mytxt = "%s: %s %s. %s !!! %s." % (
                            bold(_("INJECT")),
                            blue(str(atom)),
                            red(_("has been injected")),
                            red(_("You need to quickpkg it manually to update the embedded database")),
                            red(_("Repository database will be updated anyway")),
                        )
                        self.updateProgress(
                            mytxt,
                            importance = 1,
                            type = "warning",
                            header = darkred(" * ")
                        )

        iddeps = self.searchDependency(atomkey, like = True, multi = True)
        for iddep in iddeps:
            # update string
            mydep = self.retrieveDependencyFromIddependency(iddep)
            mydep_key = self.entropyTools.dep_getkey(mydep)
            if mydep_key != atomkey:
                continue
            if not mydep.endswith(":"+slot_from): # probably slotted dep
                continue
            mydep = mydep.replace(":"+slot_from,":"+slot_to)
            # now update
            # dependstable on server is always re-generated
            self.setDependency(iddep, mydep)
            # we have to repackage also package owning this iddep
            iddependencies_idpackages |= self.searchIdpackageFromIddependency(iddep)

        self.commitChanges()
        for idpackage_owner in iddependencies_idpackages:
            myatom = self.retrieveAtom(idpackage_owner)
            quickpkg_queue.add(myatom)
        return quickpkg_queue

    def runTreeUpdatesQuickpkgAction(self, atoms):

        branch = etpConst['branch']
        # ask branch question
        mytxt = "%s '%s' ?" % (
            _("Would you like to continue with the default branch"), # it is a question
            branch,
        )
        rc = self.askQuestion(mytxt)
        if rc == "No":
            # ask which
            while 1:
                branch = readtext("%s: " % (_("Type your branch"),)) # use the keyboard!
                if branch not in self.listAllBranches():
                    mytxt = "%s: %s: %s" % (
                        bold(_("ATTENTION")),
                        red(_("the specified branch does not exist")),
                        blue(branch),
                    )
                    self.updateProgress(
                        mytxt,
                        importance = 1,
                        type = "warning",
                        header = darkred(" * ")
                    )
                    continue
                # ask to confirm
                mytxt = "%s '%s' ?" % (_("Do you confirm"),branch,)
                rc = self.askQuestion(mytxt)
                if rc == "Yes":
                    break

        self.commitChanges()

        package_paths = set()
        runatoms = set()
        for myatom in atoms:
            mymatch = self.atomMatch(myatom)
            if mymatch[0] == -1:
                continue
            myatom = self.retrieveAtom(mymatch[0])
            runatoms.add(myatom)

        for myatom in runatoms:
            self.updateProgress(
                red("%s: " % (_("repackaging"),) )+blue(myatom),
                importance = 1,
                type = "warning",
                header = blue("  # ")
            )
            mydest = self.ServiceInterface.get_local_store_directory(self.server_repo)
            try:
                mypath = self.ServiceInterface.quickpkg(myatom,mydest)
            except:
                # remove broken bin before raising
                mypath = os.path.join(mydest,os.path.basename(myatom)+etpConst['packagesext'])
                if os.path.isfile(mypath):
                    os.remove(mypath)
                self.entropyTools.printTraceback()
                mytxt = "%s: %s: %s, %s." % (
                    bold(_("WARNING")),
                    red(_("Cannot complete quickpkg for atom")),
                    blue(myatom),
                    _("do it manually"),
                )
                self.updateProgress(
                    mytxt,
                    importance = 1,
                    type = "warning",
                    header = darkred(" * ")
                )
                continue
            package_paths.add(mypath)
        packages_data = [(x,branch,False) for x in package_paths]
        idpackages = self.ServiceInterface.add_packages_to_repository(packages_data, repo = self.server_repo)

        if not idpackages:

            mytxt = "%s: %s. %s." % (
                bold(_("ATTENTION")),
                red(_("runTreeUpdatesQuickpkgAction did not run properly")),
                red(_("Please update packages manually")),
            )
            self.updateProgress(
                mytxt,
                importance = 1,
                type = "warning",
                header = darkred(" * ")
            )

    # this function manages the submitted package
    # if it does not exist, it fires up addPackage
    # otherwise it fires up updatePackage
    def handlePackage(self, etpData, forcedRevision = -1):

        self.checkReadOnly()

        # build atom string
        versiontag = ''
        if etpData['versiontag']:
            versiontag = '#'+etpData['versiontag']

        foundid = self.isPackageAvailable(etpData['category']+"/"+etpData['name']+"-"+etpData['version']+versiontag)
        if (foundid < 0): # same atom doesn't exist in any branch
            return self.addPackage(etpData, revision = forcedRevision)
        else:
            return self.updatePackage(etpData, forcedRevision) # only when the same atom exists

    def retrieve_packages_to_remove(self, name, category, slot, branch, injected):
        removelist = set()

        # we need to find other packages with the same key and slot, and remove them
        if self.clientDatabase: # client database can't care about branch
            searchsimilar = self.searchPackagesByNameAndCategory(
                name = name,
                category = category,
                sensitive = True
            )
        else: # server supports multiple branches inside a db
            searchsimilar = self.searchPackagesByNameAndCategory(
                name = name,
                category = category,
                sensitive = True,
                branch = branch
            )

        if not injected:
            # read: if package has been injected, we'll skip
            # the removal of packages in the same slot, usually used server side btw
            for oldpkg in searchsimilar:
                # get the package slot
                idpackage = oldpkg[1]
                myslot = self.retrieveSlot(idpackage)
                isinjected = self.isInjected(idpackage)
                if isinjected:
                    continue
                    # we merely ignore packages with
                    # negative counters, since they're the injected ones
                if slot == myslot:
                    # remove!
                    removelist.add(idpackage)

        return removelist

    def addPackage(self, etpData, revision = -1, idpackage = None, do_remove = True, do_commit = True, formatted_content = False):

        self.checkReadOnly()
        self.live_cache.clear()

        if revision == -1:
            try:
                revision = int(etpData['revision'])
            except (KeyError, ValueError):
                etpData['revision'] = 0 # revision not specified
                revision = 0

        if do_remove:
            removelist = self.retrieve_packages_to_remove(
                            etpData['name'],
                            etpData['category'],
                            etpData['slot'],
                            etpConst['branch'],
                            etpData['injected']
            )
            for pkg in removelist:
                self.removePackage(pkg)

        ### create new ids

        # create new category if it doesn't exist
        catid = self.isCategoryAvailable(etpData['category'])
        if catid == -1: catid = self.addCategory(etpData['category'])

        # create new license if it doesn't exist
        licid = self.isLicenseAvailable(etpData['license'])
        if licid == -1: licid = self.addLicense(etpData['license'])

        idprotect = self.isProtectAvailable(etpData['config_protect'])
        if idprotect == -1: idprotect = self.addProtect(etpData['config_protect'])

        idprotect_mask = self.isProtectAvailable(etpData['config_protect_mask'])
        if idprotect_mask == -1: idprotect_mask = self.addProtect(etpData['config_protect_mask'])

        idflags = self.areCompileFlagsAvailable(etpData['chost'],etpData['cflags'],etpData['cxxflags'])
        if idflags == -1: idflags = self.addCompileFlags(etpData['chost'],etpData['cflags'],etpData['cxxflags'])


        # look for configured versiontag
        versiontag = ""
        if (etpData['versiontag']):
            versiontag = "#"+etpData['versiontag']

        trigger = 0
        if etpData['trigger']:
            trigger = 1

        # baseinfo
        pkgatom = etpData['category']+"/"+etpData['name']+"-"+etpData['version']+versiontag

        mybaseinfo_data = [
            pkgatom,
            catid,
            etpData['name'],
            etpData['version'],
            etpData['versiontag'],
            revision,
            etpData['branch'],
            etpData['slot'],
            licid,
            etpData['etpapi'],
            trigger
        ]

        myidpackage_string = 'NULL'
        if type(idpackage) is int:
            myidpackage_string = '?'
            mybaseinfo_data.insert(0,idpackage)
        else:
            idpackage = None
        self.cursor.execute(
                'INSERT into baseinfo VALUES '
                '('+myidpackage_string+',?,?,?,?,?,?,?,?,?,?,?)'
                , mybaseinfo_data
        )
        if idpackage == None:
            idpackage = self.cursor.lastrowid

        # extrainfo
        self.cursor.execute(
                'INSERT into extrainfo VALUES '
                '(?,?,?,?,?,?,?,?)'
                , (	idpackage,
                        etpData['description'],
                        etpData['homepage'],
                        etpData['download'],
                        etpData['size'],
                        idflags,
                        etpData['digest'],
                        etpData['datecreation'],
                        )
        )
        ### other information iserted below are not as critical as these above

        # tables using a select
        self.insertEclasses(idpackage, etpData['eclasses'])
        self.insertNeeded(idpackage, etpData['needed'])
        self.insertDependencies(idpackage, etpData['dependencies'])
        self.insertSources(idpackage, etpData['sources'])
        self.insertUseflags(idpackage, etpData['useflags'])
        self.insertKeywords(idpackage, etpData['keywords'])
        self.insertLicenses(etpData['licensedata'])
        self.insertMirrors(etpData['mirrorlinks'])

        # not depending on other tables == no select done
        self.insertContent(idpackage, etpData['content'], already_formatted = formatted_content)
        etpData['counter'] = int(etpData['counter']) # cast to integer
        etpData['counter'] = self.insertPortageCounter(
                                idpackage,
                                etpData['counter'],
                                etpData['branch'],
                                etpData['injected']
        )
        self.insertOnDiskSize(idpackage, etpData['disksize'])
        self.insertTrigger(idpackage, etpData['trigger'])
        self.insertConflicts(idpackage, etpData['conflicts'])
        self.insertProvide(idpackage, etpData['provide'])
        self.insertMessages(idpackage, etpData['messages'])
        self.insertConfigProtect(idpackage, idprotect)
        self.insertConfigProtect(idpackage, idprotect_mask, mask = True)
        # injected?
        if etpData['injected']:
            self.setInjected(idpackage, do_commit = False)
        # is it a system package?
        if etpData['systempackage']:
            self.setSystemPackage(idpackage, do_commit = False)

        self.clearCache()
        if do_commit:
            self.commitChanges()

        ### RSS Atom support
        ### dictionary will be elaborated by activator
        if etpConst['rss-feed'] and not self.clientDatabase:
            rssAtom = pkgatom+"~"+str(revision)
            # store addPackage action
            rssObj = self.dumpTools.loadobj(etpConst['rss-dump-name'])
            global etpRSSMessages
            if rssObj:
                etpRSSMessages = rssObj.copy()
            if not isinstance(etpRSSMessages,dict):
                etpRSSMessages = {}
            if not etpRSSMessages.has_key('added'):
                etpRSSMessages['added'] = {}
            if not etpRSSMessages.has_key('removed'):
                etpRSSMessages['removed'] = {}
            if rssAtom in etpRSSMessages['removed']:
                del etpRSSMessages['removed'][rssAtom]
            etpRSSMessages['added'][rssAtom] = {}
            etpRSSMessages['added'][rssAtom]['description'] = etpData['description']
            etpRSSMessages['added'][rssAtom]['homepage'] = etpData['homepage']
            etpRSSMessages['light'][rssAtom] = {}
            etpRSSMessages['light'][rssAtom]['description'] = etpData['description']
            # save
            self.dumpTools.dumpobj(etpConst['rss-dump-name'],etpRSSMessages)

        # Update category description
        if not self.clientDatabase:
            mycategory = etpData['category']
            descdata = {}
            try:
                descdata = self.get_category_description_from_disk(mycategory)
            except (IOError,OSError,EOFError):
                pass
            if descdata:
                self.setCategoryDescription(mycategory,descdata)

        return idpackage, revision, etpData

    # Update already available atom in db
    # returns True,revision if the package has been updated
    # returns False,revision if not
    def updatePackage(self, etpData, forcedRevision = -1):

        self.checkReadOnly()

        # build atom string
        versiontag = ''
        if etpData['versiontag']:
            versiontag = '#'+etpData['versiontag']
        pkgatom = etpData['category'] + "/" + etpData['name'] + "-" + etpData['version']+versiontag

        # for client database - the atom if present, must be overwritten with the new one regardless its branch
        if (self.clientDatabase):

            atomid = self.isPackageAvailable(pkgatom)
            if atomid > -1:
                self.removePackage(atomid)

            return self.addPackage(etpData, revision = forcedRevision)

        else:
            # update package in etpData['branch']
            # get its package revision
            idpackage = self.getIDPackage(pkgatom,etpData['branch'])
            if (forcedRevision == -1):
                if (idpackage != -1):
                    curRevision = self.retrieveRevision(idpackage)
                else:
                    curRevision = 0
            else:
                curRevision = forcedRevision

            if (idpackage != -1): # remove old package in branch
                self.removePackage(idpackage)
                if (forcedRevision == -1):
                    curRevision += 1

            # add the new one
            return self.addPackage(etpData, revision = curRevision)


    def removePackage(self, idpackage, do_cleanup = True, do_commit = True):

        self.checkReadOnly()
        self.live_cache.clear()

        ### RSS Atom support
        ### dictionary will be elaborated by activator
        if etpConst['rss-feed'] and not self.clientDatabase:
            # store addPackage action
            rssObj = self.dumpTools.loadobj(etpConst['rss-dump-name'])
            global etpRSSMessages
            if rssObj:
                etpRSSMessages = rssObj.copy()
            rssAtom = self.retrieveAtom(idpackage)
            rssRevision = self.retrieveRevision(idpackage)
            rssAtom += "~"+str(rssRevision)
            if not isinstance(etpRSSMessages,dict):
                etpRSSMessages = {}
            if not etpRSSMessages.has_key('added'):
                etpRSSMessages['added'] = {}
            if not etpRSSMessages.has_key('removed'):
                etpRSSMessages['removed'] = {}
            if rssAtom in etpRSSMessages['added']:
                del etpRSSMessages['added'][rssAtom]
            etpRSSMessages['removed'][rssAtom] = {}
            try:
                etpRSSMessages['removed'][rssAtom]['description'] = self.retrieveDescription(idpackage)
            except:
                etpRSSMessages['removed'][rssAtom]['description'] = "N/A"
            try:
                etpRSSMessages['removed'][rssAtom]['homepage'] = self.retrieveHomepage(idpackage)
            except:
                etpRSSMessages['removed'][rssAtom]['homepage'] = ""
            # save
            self.dumpTools.dumpobj(etpConst['rss-dump-name'],etpRSSMessages)

        idpackage = str(idpackage)
        # baseinfo
        self.cursor.execute('DELETE FROM baseinfo WHERE idpackage = '+idpackage)
        # extrainfo
        self.cursor.execute('DELETE FROM extrainfo WHERE idpackage = '+idpackage)
        # content
        self.cursor.execute('DELETE FROM content WHERE idpackage = '+idpackage)
        # dependencies
        self.cursor.execute('DELETE FROM dependencies WHERE idpackage = '+idpackage)
        # provide
        self.cursor.execute('DELETE FROM provide WHERE idpackage = '+idpackage)
        # conflicts
        self.cursor.execute('DELETE FROM conflicts WHERE idpackage = '+idpackage)
        # protect
        self.cursor.execute('DELETE FROM configprotect WHERE idpackage = '+idpackage)
        # protect_mask
        self.cursor.execute('DELETE FROM configprotectmask WHERE idpackage = '+idpackage)
        # sources
        self.cursor.execute('DELETE FROM sources WHERE idpackage = '+idpackage)
        # useflags
        self.cursor.execute('DELETE FROM useflags WHERE idpackage = '+idpackage)
        # keywords
        self.cursor.execute('DELETE FROM keywords WHERE idpackage = '+idpackage)

        #
        # WARNING: exception won't be handled anymore with 1.0
        #

        try:
            # messages
            self.cursor.execute('DELETE FROM messages WHERE idpackage = '+idpackage)
        except:
            pass
        try:
            # systempackage
            self.cursor.execute('DELETE FROM systempackages WHERE idpackage = '+idpackage)
        except:
            pass
        try:
            # counter
            self.cursor.execute('DELETE FROM counters WHERE idpackage = '+idpackage)
        except:
            if (self.dbname == etpConst['clientdbid']) or self.dbname.startswith(etpConst['serverdbid']):
                raise
        try:
            # on disk sizes
            self.cursor.execute('DELETE FROM sizes WHERE idpackage = '+idpackage)
        except:
            pass
        try:
            # eclasses
            self.cursor.execute('DELETE FROM eclasses WHERE idpackage = '+idpackage)
        except:
            pass
        try:
            # needed
            self.cursor.execute('DELETE FROM needed WHERE idpackage = '+idpackage)
        except:
            pass
        try:
            # triggers
            self.cursor.execute('DELETE FROM triggers WHERE idpackage = '+idpackage)
        except:
            pass
        try:
            # inject table
            self.cursor.execute('DELETE FROM injected WHERE idpackage = '+idpackage)
        except:
            pass

        # Remove from installedtable if exists
        self.removePackageFromInstalledTable(idpackage)
        # Remove from dependstable if exists
        self.removePackageFromDependsTable(idpackage)

        if do_cleanup:
            # Cleanups if at least one package has been removed
            self.doCleanups()
            # clear caches
            self.clearCache()

        if do_commit:
            self.commitChanges()

    def removeMirrorEntries(self,mirrorname):
        self.cursor.execute('DELETE FROM mirrorlinks WHERE mirrorname = "'+mirrorname+'"')

    def addMirrors(self,mirrorname,mirrorlist):
        for x in mirrorlist:
            self.cursor.execute(
                'INSERT into mirrorlinks VALUES '
                '(?,?)', (mirrorname,x,)
            )

    def addCategory(self,category):
        self.cursor.execute(
                'INSERT into categories VALUES '
                '(NULL,?)', (category,)
        )
        return self.cursor.lastrowid

    def addProtect(self,protect):
        self.cursor.execute(
                'INSERT into configprotectreference VALUES '
                '(NULL,?)', (protect,)
        )
        return self.cursor.lastrowid

    def addSource(self,source):
        self.cursor.execute(
                'INSERT into sourcesreference VALUES '
                '(NULL,?)', (source,)
        )
        return self.cursor.lastrowid

    def addDependency(self,dependency):
        self.cursor.execute(
                'INSERT into dependenciesreference VALUES '
                '(NULL,?)', (dependency,)
        )
        return self.cursor.lastrowid

    def addKeyword(self,keyword):
        self.cursor.execute(
                'INSERT into keywordsreference VALUES '
                '(NULL,?)', (keyword,)
        )
        return self.cursor.lastrowid

    def addUseflag(self,useflag):
        self.cursor.execute(
                'INSERT into useflagsreference VALUES '
                '(NULL,?)', (useflag,)
        )
        return self.cursor.lastrowid

    def addEclass(self,eclass):
        self.cursor.execute(
                'INSERT into eclassesreference VALUES '
                '(NULL,?)', (eclass,)
        )
        return self.cursor.lastrowid

    def addNeeded(self,needed):
        self.cursor.execute(
                'INSERT into neededreference VALUES '
                '(NULL,?)', (needed,)
        )
        return self.cursor.lastrowid

    def addLicense(self,pkglicense):
        if not self.entropyTools.is_valid_string(pkglicense):
            pkglicense = ' ' # workaround for broken license entries
        self.cursor.execute(
                'INSERT into licenses VALUES '
                '(NULL,?)', (pkglicense,)
        )
        return self.cursor.lastrowid

    def addCompileFlags(self,chost,cflags,cxxflags):
        self.cursor.execute(
                'INSERT into flags VALUES '
                '(NULL,?,?,?)', (chost,cflags,cxxflags,)
        )
        return self.cursor.lastrowid

    def setSystemPackage(self, idpackage, do_commit = True):
        self.checkReadOnly()
        self.cursor.execute('INSERT into systempackages VALUES (?)', (idpackage,))
        if do_commit:
            self.commitChanges()

    def setInjected(self, idpackage, do_commit = True):
        self.checkReadOnly()
        if not self.isInjected(idpackage):
            self.cursor.execute(
                'INSERT into injected VALUES '
                '(?)'
                , ( idpackage, )
            )
        if do_commit:
            self.commitChanges()

    # date expressed the unix way
    def setDateCreation(self, idpackage, date):
        self.checkReadOnly()
        self.cursor.execute('UPDATE extrainfo SET datecreation = (?) WHERE idpackage = (?)', (str(date),idpackage,))
        self.commitChanges()

    def setDigest(self, idpackage, digest):
        self.checkReadOnly()
        self.cursor.execute('UPDATE extrainfo SET digest = (?) WHERE idpackage = (?)', (digest,idpackage,))
        self.commitChanges()

    def setDownloadURL(self, idpackage, url):
        self.checkReadOnly()
        self.cursor.execute('UPDATE extrainfo SET download = (?) WHERE idpackage = (?)', (url,idpackage,))
        self.commitChanges()

    def setCategory(self, idpackage, category):
        self.checkReadOnly()
        # create new category if it doesn't exist
        catid = self.isCategoryAvailable(category)
        if (catid == -1):
            # create category
            catid = self.addCategory(category)
        self.cursor.execute('UPDATE baseinfo SET idcategory = (?) WHERE idpackage = (?)', (catid,idpackage,))
        self.commitChanges()

    def setCategoryDescription(self, category, description_data):
        self.checkReadOnly()
        self.cursor.execute('DELETE FROM categoriesdescription WHERE category = (?)', (category,))
        for locale in description_data:
            mydesc = description_data[locale]
            #if type(mydesc) is unicode:
            #    mydesc = mydesc.encode('raw_unicode_escape')
            self.cursor.execute('INSERT INTO categoriesdescription VALUES (?,?,?)', (category,locale,mydesc,))
        self.commitChanges()

    def setName(self, idpackage, name):
        self.checkReadOnly()
        self.cursor.execute('UPDATE baseinfo SET name = (?) WHERE idpackage = (?)', (name,idpackage,))
        self.commitChanges()

    def setDependency(self, iddependency, dependency):
        self.checkReadOnly()
        self.cursor.execute('UPDATE dependenciesreference SET dependency = (?) WHERE iddependency = (?)', (dependency,iddependency,))
        self.commitChanges()

    def setAtom(self, idpackage, atom):
        self.checkReadOnly()
        self.cursor.execute('UPDATE baseinfo SET atom = (?) WHERE idpackage = (?)', (atom,idpackage,))
        self.commitChanges()

    def setSlot(self, idpackage, slot):
        self.checkReadOnly()
        self.cursor.execute('UPDATE baseinfo SET slot = (?) WHERE idpackage = (?)', (slot,idpackage,))
        self.commitChanges()

    def removeLicensedata(self, license_name):
        if not self.doesTableExist("licensedata"):
            return
        self.cursor.execute('DELETE FROM licensedata WHERE licensename = (?)', (license_name,))

    def removeDependencies(self, idpackage):
        self.checkReadOnly()
        self.cursor.execute("DELETE FROM dependencies WHERE idpackage = (?)", (idpackage,))
        self.commitChanges()

    def insertDependencies(self, idpackage, depdata):

        dcache = set()
        deps = set()
        for dep in depdata:

            if dep in dcache:
                continue

            iddep = self.isDependencyAvailable(dep)
            if (iddep == -1):
                # create category
                iddep = self.addDependency(dep)

            if type(depdata) is dict:
                deptype = depdata[dep]
            else:
                deptype = 0

            deps.add((idpackage,iddep,deptype,))
            dcache.add(dep)

        def myiter():
            for item in deps:
                yield item

        self.cursor.executemany('INSERT into dependencies VALUES (?,?,?)', myiter())

    def removeContent(self, idpackage):
        self.checkReadOnly()
        self.cursor.execute("DELETE FROM content WHERE idpackage = (?)", (idpackage,))
        self.commitChanges()

    def insertContent(self, idpackage, content, already_formatted = False):

        do_encode = False
        for item in content:
            if type(item) is unicode:
                do_encode = True
                break

        def myiter():
            for xfile in content:
                contenttype = content[xfile]
                if do_encode:
                    xfile = xfile.encode('raw_unicode_escape')
                yield (idpackage,xfile,contenttype,)

        if already_formatted:
            self.cursor.executemany('INSERT INTO content VALUES (?,?,?)',content)
        else:
            self.cursor.executemany('INSERT INTO content VALUES (?,?,?)',myiter())

    def insertLicenses(self, licenses_data):

        mylicenses = licenses_data.keys()
        mydata = []
        for mylicense in mylicenses:
            found = self.isLicensedataKeyAvailable(mylicense)
            if found:
                continue
            mydata.append((mylicense,buffer(licenses_data[mylicense]),0,))

        def myiter():
            for item in mydata:
                yield item

        self.cursor.executemany('INSERT into licensedata VALUES (?,?,?)',myiter())

    def insertConfigProtect(self, idpackage, idprotect, mask = False):

        mytable = 'configprotect'
        if mask: mytable += 'mask'
        self.cursor.execute('INSERT into '+mytable+' VALUES (?,?)', (idpackage,idprotect,))


    def insertMirrors(self, mirrors):

        for mirrorname,mirrorlist in mirrors:
            # remove old
            self.removeMirrorEntries(mirrorname)
            # add new
            self.addMirrors(mirrorname,mirrorlist)

    def insertKeywords(self, idpackage, keywords):

        mydata = set()
        for key in keywords:
            idkeyword = self.isKeywordAvailable(key)
            if (idkeyword == -1):
                # create category
                idkeyword = self.addKeyword(key)
            mydata.add((idpackage,idkeyword,))

        def myiter():
            for item in mydata:
                yield item

        self.cursor.executemany('INSERT into keywords VALUES (?,?)',myiter())

    def insertUseflags(self, idpackage, useflags):

        mydata = set()
        for flag in useflags:
            iduseflag = self.isUseflagAvailable(flag)
            if (iduseflag == -1):
                # create category
                iduseflag = self.addUseflag(flag)
            mydata.add((idpackage,iduseflag,))

        def myiter():
            for item in mydata:
                yield item

        self.cursor.executemany('INSERT into useflags VALUES (?,?)',myiter())

    def insertSources(self, idpackage, sources):

        mydata = set()
        for source in sources:
            if (not source) or (source == "") or (not self.entropyTools.is_valid_string(source)):
                continue
            idsource = self.isSourceAvailable(source)
            if (idsource == -1):
                # create category
                idsource = self.addSource(source)
            mydata.add((idpackage,idsource,))

        def myiter():
            for item in mydata:
                yield item

        self.cursor.executemany('INSERT into sources VALUES (?,?)',myiter())

    def insertConflicts(self, idpackage, conflicts):

        def myiter():
            for conflict in conflicts:
                yield (idpackage,conflict,)

        self.cursor.executemany('INSERT into conflicts VALUES (?,?)',myiter())

    def insertMessages(self, idpackage, messages):

        def myiter():
            for message in messages:
                yield (idpackage,message,)

        self.cursor.executemany('INSERT into messages VALUES (?,?)',myiter())

    def insertProvide(self, idpackage, provides):

        def myiter():
            for atom in provides:
                yield (idpackage,atom,)

        self.cursor.executemany('INSERT into provide VALUES (?,?)',myiter())

    def insertNeeded(self, idpackage, neededs):

        mydata = set()
        for needed,elfclass in neededs:
            idneeded = self.isNeededAvailable(needed)
            if idneeded == -1:
                # create eclass
                idneeded = self.addNeeded(needed)
            mydata.add((idpackage,idneeded,elfclass))

        def myiter():
            for item in mydata:
                yield item

        self.cursor.executemany('INSERT into needed VALUES (?,?,?)',myiter())

    def insertEclasses(self, idpackage, eclasses):

        mydata = set()
        for eclass in eclasses:
            idclass = self.isEclassAvailable(eclass)
            if (idclass == -1):
                # create eclass
                idclass = self.addEclass(eclass)
            mydata.add((idpackage,idclass))

        def myiter():
            for item in mydata:
                yield item

        self.cursor.executemany('INSERT into eclasses VALUES (?,?)',myiter())

    def insertOnDiskSize(self, idpackage, mysize):
        self.cursor.execute('INSERT into sizes VALUES (?,?)', (idpackage,mysize,))

    def insertTrigger(self, idpackage, trigger):
        self.cursor.execute('INSERT into triggers VALUES (?,?)', (idpackage,buffer(trigger),))

    def insertPortageCounter(self, idpackage, counter, branch, injected):

        if (counter != -1) and not injected:

            if counter <= -2:
                # special cases
                counter = self.getNewNegativeCounter()

            try:
                self.cursor.execute(
                'INSERT into counters VALUES '
                '(?,?,?)'
                , ( counter,
                    idpackage,
                    branch,
                    )
                )
            except self.dbapi2.IntegrityError: # we have a PRIMARY KEY we need to remove
                self.migrateCountersTable()
                self.cursor.execute(
                'INSERT into counters VALUES '
                '(?,?,?)'
                , ( counter,
                    idpackage,
                    branch,
                    )
                )
            except:
                if self.dbname == etpConst['clientdbid']: # force only for client database
                    if self.doesTableExist("counters"):
                        raise
                    self.cursor.execute(
                    'INSERT into counters VALUES '
                    '(?,?,?)'
                    , ( counter,
                        idpackage,
                        branch,
                        )
                    )
                elif self.dbname.startswith(etpConst['serverdbid']):
                    raise

        return counter

    def insertCounter(self, idpackage, counter, branch = None):
        self.checkReadOnly()
        if not branch:
            branch = etpConst['branch']
        self.cursor.execute('DELETE FROM counters WHERE counter = (?) OR idpackage = (?)', (counter,idpackage,))
        self.cursor.execute('INSERT INTO counters VALUES (?,?,?)', (counter,idpackage,branch,))
        self.commitChanges()

    def setTrashedCounter(self, counter):
        self.checkReadOnly()
        self.cursor.execute('DELETE FROM trashedcounters WHERE counter = (?)', (counter,))
        self.cursor.execute('INSERT INTO trashedcounters VALUES (?)', (counter,))
        self.commitChanges()

    def setCounter(self, idpackage, counter, branch = None):
        self.checkReadOnly()

        branchstring = ''
        insertdata = [counter,idpackage]
        if branch:
            branchstring = ', branch = (?)'
            insertdata.insert(1,branch)
        else:
            branch = etpConst['branch']

        try:
            self.cursor.execute('UPDATE counters SET counter = (?) '+branchstring+' WHERE idpackage = (?)', insertdata)
        except:
            if self.dbname == etpConst['clientdbid']:
                raise
        self.commitChanges()

    def contentDiff(self, idpackage, dbconn, dbconn_idpackage):
        self.checkReadOnly()
        self.connection.text_factory = lambda x: unicode(x, "raw_unicode_escape")
        # create a random table and fill
        randomtable = "cdiff"+str(self.entropyTools.getRandomNumber())
        while self.doesTableExist(randomtable):
            randomtable = "cdiff"+str(self.entropyTools.getRandomNumber())
        self.cursor.execute('CREATE TEMPORARY TABLE '+randomtable+' ( file VARCHAR )')

        dbconn.connection.text_factory = lambda x: unicode(x, "raw_unicode_escape")
        dbconn.cursor.execute('select file from content where idpackage = (?)', (dbconn_idpackage,))
        xfile = dbconn.cursor.fetchone()
        while xfile:
            self.cursor.execute('INSERT INTO '+randomtable+' VALUES (?)', (xfile[0],))
            xfile = dbconn.cursor.fetchone()

        # now compare
        self.cursor.execute('SELECT file FROM content WHERE content.idpackage = (?) AND content.file NOT IN (SELECT file from '+randomtable+') ', (idpackage,))
        diff = self.fetchall2set(self.cursor.fetchall())
        self.cursor.execute('DROP TABLE IF EXISTS '+randomtable)
        return diff

    def doCleanups(self):
        self.cleanupUseflags()
        self.cleanupSources()
        self.cleanupEclasses()
        self.cleanupNeeded()
        self.cleanupDependencies()

    def cleanupUseflags(self):
        self.checkReadOnly()
        self.cursor.execute('delete from useflagsreference where idflag IN (select idflag from useflagsreference where idflag NOT in (select idflag from useflags))')
        self.commitChanges()

    def cleanupSources(self):
        self.checkReadOnly()
        self.cursor.execute('delete from sourcesreference where idsource IN (select idsource from sourcesreference where idsource NOT in (select idsource from sources))')
        self.commitChanges()

    def cleanupEclasses(self):
        self.checkReadOnly()
        self.cursor.execute('delete from eclassesreference where idclass IN (select idclass from eclassesreference where idclass NOT in (select idclass from eclasses))')
        self.commitChanges()

    def cleanupNeeded(self):
        self.checkReadOnly()
        self.cursor.execute('delete from neededreference where idneeded IN (select idneeded from neededreference where idneeded NOT in (select idneeded from needed))')
        self.commitChanges()

    def cleanupDependencies(self):
        self.checkReadOnly()
        self.cursor.execute('delete from dependenciesreference where iddependency IN (select iddependency from dependenciesreference where iddependency NOT in (select iddependency from dependencies))')
        self.commitChanges()

    def getNewNegativeCounter(self):
        counter = -2
        try:
            self.cursor.execute('SELECT min(counter) FROM counters')
            dbcounter = self.cursor.fetchone()
            mycounter = 0
            if dbcounter:
                mycounter = dbcounter[0]

            if mycounter >= -1:
                counter = -2
            else:
                counter = mycounter-1

        except:
            pass
        return counter

    def getApi(self):
        self.cursor.execute('SELECT max(etpapi) FROM baseinfo')
        api = self.cursor.fetchone()
        if api: api = api[0]
        else: api = -1
        return api

    def getCategory(self, idcategory):
        self.cursor.execute('SELECT category from categories WHERE idcategory = (?)', (idcategory,))
        cat = self.cursor.fetchone()
        if cat: cat = cat[0]
        return cat

    def get_category_description_from_disk(self, category):
        if not self.ServiceInterface:
            return {}
        return self.ServiceInterface.SpmService.get_category_description_data(category)

    def getIDPackage(self, atom, branch = etpConst['branch']):
        self.cursor.execute('SELECT idpackage FROM baseinfo WHERE atom = "'+atom+'" AND branch = "'+branch+'"')
        idpackage = -1
        idpackage = self.cursor.fetchone()
        if idpackage:
            idpackage = idpackage[0]
        else:
            idpackage = -1
        return idpackage

    def getIDPackageFromDownload(self, file, branch = etpConst['branch']):
        self.cursor.execute('SELECT baseinfo.idpackage FROM content,baseinfo WHERE content.file = (?) and baseinfo.branch = (?)', (file,branch,))
        idpackage = self.cursor.fetchone()
        if idpackage:
            idpackage = idpackage[0]
        else:
            idpackage = -1
        return idpackage

    def getIDPackagesFromFile(self, file):
        self.cursor.execute('SELECT idpackage FROM content WHERE file = "'+file+'"')
        idpackages = []
        for row in self.cursor:
            idpackages.append(row[0])
        return idpackages

    def getIDCategory(self, category):
        self.cursor.execute('SELECT "idcategory" FROM categories WHERE category = "'+str(category)+'"')
        idcat = -1
        for row in self.cursor:
            idcat = int(row[0])
            break
        return idcat

    def getStrictData(self, idpackage):
        self.cursor.execute('SELECT categories.category || "/" || baseinfo.name,baseinfo.slot,baseinfo.version,baseinfo.versiontag,baseinfo.revision,baseinfo.atom FROM baseinfo,categories WHERE baseinfo.idpackage = (?) and baseinfo.idcategory = categories.idcategory', (idpackage,))
        return self.cursor.fetchone()

    def getScopeData(self, idpackage):
        self.cursor.execute("""
                SELECT 
                        baseinfo.atom,
                        categories.category,
                        baseinfo.name,
                        baseinfo.version,
                        baseinfo.slot,
                        baseinfo.versiontag,
                        baseinfo.revision,
                        baseinfo.branch
                FROM 
                        baseinfo,
                        categories
                WHERE 
                        baseinfo.idpackage = (?)
                        and baseinfo.idcategory = categories.idcategory
        """, (idpackage,))
        return self.cursor.fetchone()

    def getBaseData(self,idpackage):

        sql = """
                SELECT 
                        baseinfo.atom,
                        baseinfo.name,
                        baseinfo.version,
                        baseinfo.versiontag,
                        extrainfo.description,
                        categories.category,
                        flags.chost,
                        flags.cflags,
                        flags.cxxflags,
                        extrainfo.homepage,
                        licenses.license,
                        baseinfo.branch,
                        extrainfo.download,
                        extrainfo.digest,
                        baseinfo.slot,
                        baseinfo.etpapi,
                        extrainfo.datecreation,
                        extrainfo.size,
                        baseinfo.revision
                FROM 
                        baseinfo,
                        extrainfo,
                        categories,
                        flags,
                        licenses
                WHERE 
                        baseinfo.idpackage = (?) 
                        and baseinfo.idpackage = extrainfo.idpackage 
                        and baseinfo.idcategory = categories.idcategory 
                        and extrainfo.idflags = flags.idflags
                        and baseinfo.idlicense = licenses.idlicense
        """
        self.cursor.execute(sql, (idpackage,))
        return self.cursor.fetchone()

    def getTriggerInfo(self, idpackage):
        data = {}

        mydata = self.getScopeData(idpackage)

        data['atom'] = mydata[0]
        data['category'] = mydata[1]
        data['name'] = mydata[2]
        data['version'] = mydata[3]
        data['versiontag'] = mydata[5]
        flags = self.retrieveCompileFlags(idpackage)
        data['chost'] = flags[0]
        data['cflags'] = flags[1]
        data['cxxflags'] = flags[2]

        data['trigger'] = self.retrieveTrigger(idpackage)
        data['eclasses'] = self.retrieveEclasses(idpackage)
        data['content'] = self.retrieveContent(idpackage)

        return data

    def getPackageData(self, idpackage, get_content = True, content_insert_formatted = False, trigger_unicode = False):
        data = {}

        try:
            data['atom'], data['name'], data['version'], data['versiontag'], \
            data['description'], data['category'], data['chost'], \
            data['cflags'], data['cxxflags'],data['homepage'], \
            data['license'], data['branch'], data['download'], \
            data['digest'], data['slot'], data['etpapi'], \
            data['datecreation'], data['size'], data['revision']  = self.getBaseData(idpackage)
        except TypeError:
            return None

        ### risky to add to the sql above
        data['counter'] = self.retrieveCounter(idpackage)
        data['messages'] = self.retrieveMessages(idpackage)
        data['trigger'] = self.retrieveTrigger(idpackage, get_unicode = trigger_unicode)
        data['disksize'] = self.retrieveOnDiskSize(idpackage)

        data['injected'] = self.isInjected(idpackage)
        data['systempackage'] = False
        if self.isSystemPackage(idpackage):
            data['systempackage'] = True

        data['config_protect'] = self.retrieveProtect(idpackage)
        data['config_protect_mask'] = self.retrieveProtectMask(idpackage)
        data['useflags'] = self.retrieveUseflags(idpackage)
        data['keywords'] = self.retrieveKeywords(idpackage)
        data['sources'] = self.retrieveSources(idpackage)
        data['eclasses'] = self.retrieveEclasses(idpackage)
        data['needed'] = self.retrieveNeeded(idpackage, extended = True)
        data['provide'] = self.retrieveProvide(idpackage)
        data['conflicts'] = self.retrieveConflicts(idpackage)
        data['licensedata'] = self.retrieveLicensedata(idpackage)

        mirrornames = set()
        for x in data['sources']:
            if x.startswith("mirror://"):
                mirrornames.add(x.split("/")[2])
        data['mirrorlinks'] = []
        for mirror in mirrornames:
            data['mirrorlinks'].append([mirror,self.retrieveMirrorInfo(mirror)])

        data['content'] = {}
        if get_content:
            data['content'] = self.retrieveContent(
                idpackage,
                extended = True,
                formatted = True,
                insert_formatted = content_insert_formatted
            )

        mydeps = {}
        depdata = self.retrieveDependencies(idpackage, extended = True)
        for dep,deptype in depdata:
            mydeps[dep] = deptype
        data['dependencies'] = mydeps

        return data

    def fetchall2set(self, item):
        mycontent = set()
        for x in item:
            mycontent |= set(x)
        return mycontent

    def fetchall2list(self, item):
        content = []
        for x in item:
            content += list(x)
        return content

    def fetchone2list(self, item):
        return list(item)

    def fetchone2set(self, item):
        return set(item)

    def clearCache(self, depends = False):
        self.live_cache.clear()
        def do_clear(name):
            dump_path = os.path.join(etpConst['dumpstoragedir'],name)
            dump_dir = os.path.dirname(dump_path)
            if os.path.isdir(dump_dir):
                for item in os.listdir(dump_dir):
                    item = os.path.join(dump_dir,item)
                    if os.path.isfile(item):
                        os.remove(item)
        do_clear(etpCache['dbMatch']+"/"+self.dbname+"/")
        do_clear(etpCache['dbSearch']+"/"+self.dbname+"/")
        if depends:
            do_clear(etpCache['depends_tree'])
            do_clear(etpCache['dep_tree'])
            do_clear(etpCache['filter_satisfied_deps'])

    def fetchSearchCache(self, key, function, extra_hash = 0):
        if self.xcache:

            c_hash = str(hash(function)) + str(extra_hash)
            c_match = str(key)
            try:
                cached = self.dumpTools.loadobj(etpCache['dbSearch']+"/"+self.dbname+"/"+c_match+"/"+c_hash)
                if cached != None:
                    return cached
            except EOFError:
                pass

    def storeSearchCache(self, key, function, search_cache_data, extra_hash = 0):
        if self.xcache:
            c_hash = str(hash(function)) + str(extra_hash)
            c_match = str(key)
            try:
                sperms = False
                if not os.path.isdir(os.path.join(etpConst['dumpstoragedir'],etpCache['dbSearch'],self.dbname)):
                    sperms = True
                elif not os.path.isdir(os.path.join(etpConst['dumpstoragedir'],etpCache['dbSearch'],self.dbname,c_match)):
                    sperms = True
                self.dumpTools.dumpobj(etpCache['dbSearch']+"/"+self.dbname+"/"+c_match+"/"+c_hash,search_cache_data)
                if sperms:
                    const_setup_perms(os.path.join(etpConst['dumpstoragedir'],etpCache['dbSearch']),etpConst['entropygid'])
            except IOError:
                pass

    def retrieveRepositoryUpdatesDigest(self, repository):
        if not self.doesTableExist("treeupdates"):
            return -1
        self.cursor.execute('SELECT digest FROM treeupdates WHERE repository = (?)', (repository,))
        mydigest = self.cursor.fetchone()
        if mydigest:
            return mydigest[0]
        else:
            return -1

    def listAllTreeUpdatesActions(self, no_ids_repos = False):
        if no_ids_repos:
            self.cursor.execute('SELECT command,branch,date FROM treeupdatesactions')
        else:
            self.cursor.execute('SELECT * FROM treeupdatesactions')
        return self.cursor.fetchall()

    def retrieveTreeUpdatesActions(self, repository, forbranch = etpConst['branch']):
        if not self.doesTableExist("treeupdatesactions"):
            return set()
        self.cursor.execute('SELECT command FROM treeupdatesactions where repository = (?) and branch = (?) order by date', (repository,forbranch))
        return self.fetchall2list(self.cursor.fetchall())

    # mainly used to restore a previous table, used by reagent in --initialize
    def bumpTreeUpdatesActions(self, updates):
        self.checkReadOnly()
        for update in updates:
            self.cursor.execute('INSERT INTO treeupdatesactions VALUES (?,?,?,?,?)', update)
        self.commitChanges()

    def removeTreeUpdatesActions(self, repository):
        self.checkReadOnly()
        self.cursor.execute('DELETE FROM treeupdatesactions WHERE repository = (?)', (repository,))
        self.commitChanges()

    def insertTreeUpdatesActions(self, updates, repository):
        self.checkReadOnly()
        for update in updates:
            update = list(update)
            update.insert(0,repository)
            self.cursor.execute('INSERT INTO treeupdatesactions VALUES (NULL,?,?,?,?)', update)
        self.commitChanges()

    def setRepositoryUpdatesDigest(self, repository, digest):
        self.checkReadOnly()
        self.cursor.execute('DELETE FROM treeupdates where repository = (?)', (repository,)) # doing it for safety
        self.cursor.execute('INSERT INTO treeupdates VALUES (?,?)', (repository,digest,))
        self.commitChanges()

    def addRepositoryUpdatesActions(self, repository, actions, forbranch = etpConst['branch']):
        self.checkReadOnly()
        mytime = str(self.entropyTools.getCurrentUnixTime())
        for command in actions:
            self.cursor.execute('INSERT INTO treeupdatesactions VALUES (NULL,?,?,?,?)', (repository,command,forbranch,mytime,))
        self.commitChanges()

    def retrieveAtom(self, idpackage):
        self.cursor.execute('SELECT atom FROM baseinfo WHERE idpackage = (?)', (idpackage,))
        atom = self.cursor.fetchone()
        if atom:
            return atom[0]

    def retrieveBranch(self, idpackage):
        self.cursor.execute('SELECT branch FROM baseinfo WHERE idpackage = (?)', (idpackage,))
        br = self.cursor.fetchone()
        if br:
            return br[0]

    def retrieveTrigger(self, idpackage, get_unicode = False):
        self.cursor.execute('SELECT data FROM triggers WHERE idpackage = (?)', (idpackage,))
        trigger = self.cursor.fetchone()
        if trigger:
            trigger = trigger[0]
        else:
            trigger = ''
        if get_unicode:
            trigger = unicode(trigger,'raw_unicode_escape')
        return trigger

    def retrieveDownloadURL(self, idpackage):
        self.cursor.execute('SELECT download FROM extrainfo WHERE idpackage = (?)', (idpackage,))
        download = self.cursor.fetchone()
        if download:
            return download[0]

    def retrieveDescription(self, idpackage):
        self.cursor.execute('SELECT description FROM extrainfo WHERE idpackage = (?)', (idpackage,))
        description = self.cursor.fetchone()
        if description:
            return description[0]

    def retrieveHomepage(self, idpackage):
        self.cursor.execute('SELECT homepage FROM extrainfo WHERE idpackage = (?)', (idpackage,))
        home = self.cursor.fetchone()
        if home:
            return home[0]

    def retrieveCounter(self, idpackage):
        counter = -1
        self.cursor.execute('SELECT counter FROM counters WHERE idpackage = (?)', (idpackage,))
        mycounter = self.cursor.fetchone()
        if mycounter:
            return mycounter[0]
        return counter

    def retrieveMessages(self, idpackage):
        messages = []
        try:
            self.cursor.execute('SELECT message FROM messages WHERE idpackage = (?)', (idpackage,))
            messages = self.fetchall2list(self.cursor.fetchall())
        except:
            pass
        return messages

    # in bytes
    def retrieveSize(self, idpackage):
        self.cursor.execute('SELECT size FROM extrainfo WHERE idpackage = (?)', (idpackage,))
        size = self.cursor.fetchone()
        if size:
            return size[0]

    # in bytes
    def retrieveOnDiskSize(self, idpackage):
        self.cursor.execute('SELECT size FROM sizes WHERE idpackage = (?)', (idpackage,))
        size = self.cursor.fetchone() # do not use [0]!
        if not size:
            size = 0
        else:
            size = size[0]
        return size

    def retrieveDigest(self, idpackage):
        self.cursor.execute('SELECT digest FROM extrainfo WHERE idpackage = (?)', (idpackage,))
        digest = self.cursor.fetchone()
        if digest:
            return digest[0]

    def retrieveName(self, idpackage):
        self.cursor.execute('SELECT name FROM baseinfo WHERE idpackage = (?)', (idpackage,))
        name = self.cursor.fetchone()
        if name:
            return name[0]

    def retrieveKeySlot(self, idpackage):
        self.cursor.execute('SELECT categories.category || "/" || baseinfo.name,baseinfo.slot FROM baseinfo,categories WHERE baseinfo.idpackage = (?) and baseinfo.idcategory = categories.idcategory', (idpackage,))
        data = self.cursor.fetchone()
        return data

    def retrieveVersion(self, idpackage):
        self.cursor.execute('SELECT version FROM baseinfo WHERE idpackage = (?)', (idpackage,))
        ver = self.cursor.fetchone()
        if ver:
            return ver[0]

    def retrieveRevision(self, idpackage):
        self.cursor.execute('SELECT revision FROM baseinfo WHERE idpackage = (?)', (idpackage,))
        rev = self.cursor.fetchone()
        if rev:
            return rev[0]

    def retrieveDateCreation(self, idpackage):
        self.cursor.execute('SELECT datecreation FROM extrainfo WHERE idpackage = (?)', (idpackage,))
        date = self.cursor.fetchone()
        if date:
            return date[0]

    def retrieveApi(self, idpackage):
        self.cursor.execute('SELECT etpapi FROM baseinfo WHERE idpackage = (?)', (idpackage,))
        api = self.cursor.fetchone()
        if api:
            return api[0]

    def retrieveUseflags(self, idpackage):
        self.cursor.execute('SELECT flagname FROM useflags,useflagsreference WHERE useflags.idpackage = (?) and useflags.idflag = useflagsreference.idflag', (idpackage,))
        return self.fetchall2set(self.cursor.fetchall())

    def retrieveEclasses(self, idpackage):
        self.cursor.execute('SELECT classname FROM eclasses,eclassesreference WHERE eclasses.idpackage = (?) and eclasses.idclass = eclassesreference.idclass', (idpackage,))
        return self.fetchall2set(self.cursor.fetchall())

    def retrieveNeeded(self, idpackage, extended = False, format = False):

        if extended and not self.doesColumnInTableExist("needed","elfclass"):
            return {}

        if extended:
            self.cursor.execute('SELECT library,elfclass FROM needed,neededreference WHERE needed.idpackage = (?) and needed.idneeded = neededreference.idneeded order by library', (idpackage,))
            needed = self.cursor.fetchall()
            needed.sort()
        else:
            self.cursor.execute('SELECT library FROM needed,neededreference WHERE needed.idpackage = (?) and needed.idneeded = neededreference.idneeded order by library', (idpackage,))
            needed = self.fetchall2list(self.cursor.fetchall())

        if extended and format:
            data = {}
            for lib,elfclass in needed:
                data[lib] = elfclass
            needed = data

        return needed

    def retrieveConflicts(self, idpackage):
        self.cursor.execute('SELECT conflict FROM conflicts WHERE idpackage = (?)', (idpackage,))
        return self.fetchall2set(self.cursor.fetchall())

    def retrieveProvide(self, idpackage):
        self.cursor.execute('SELECT atom FROM provide WHERE idpackage = (?)', (idpackage,))
        return self.fetchall2set(self.cursor.fetchall())

    def retrieveDependenciesList(self, idpackage):
        deps = self.retrieveDependencies(idpackage)
        conflicts = self.retrieveConflicts(idpackage)
        for x in conflicts:
            if x[0] != "!":
                x = "!"+x
            deps.add(x)
        return deps

    def retrieveDependencies(self, idpackage, extended = False, deptype = None):

        searchdata = [idpackage]

        depstring = ''
        if deptype != None:
            depstring = ' and dependencies.type = (?)'
            searchdata.append(deptype)

        if extended:
            self.cursor.execute('SELECT dependenciesreference.dependency,dependencies.type FROM dependencies,dependenciesreference WHERE dependencies.idpackage = (?) and dependencies.iddependency = dependenciesreference.iddependency'+depstring, searchdata)
            deps = self.cursor.fetchall()
        else:
            self.cursor.execute('SELECT dependenciesreference.dependency FROM dependencies,dependenciesreference WHERE dependencies.idpackage = (?) and dependencies.iddependency = dependenciesreference.iddependency'+depstring, searchdata)
            deps = self.fetchall2set(self.cursor.fetchall())

        return deps

    def retrieveIdDependencies(self, idpackage):
        self.cursor.execute('SELECT iddependency FROM dependencies WHERE idpackage = (?)', (idpackage,))
        return self.fetchall2set(self.cursor.fetchall())

    def retrieveDependencyFromIddependency(self, iddependency):
        self.cursor.execute('SELECT dependency FROM dependenciesreference WHERE iddependency = (?)', (iddependency,))
        dep = self.cursor.fetchone()
        if dep: dep = dep[0]
        return dep

    def retrieveKeywords(self, idpackage):
        self.cursor.execute('SELECT keywordname FROM keywords,keywordsreference WHERE keywords.idpackage = (?) and keywords.idkeyword = keywordsreference.idkeyword', (idpackage,))
        return self.fetchall2set(self.cursor.fetchall())

    def retrieveProtect(self, idpackage):
        self.cursor.execute('SELECT protect FROM configprotect,configprotectreference WHERE configprotect.idpackage = (?) and configprotect.idprotect = configprotectreference.idprotect', (idpackage,))
        protect = self.cursor.fetchone()
        if not protect:
            protect = ''
        else:
            protect = protect[0]
        return protect

    def retrieveProtectMask(self, idpackage):
        self.cursor.execute('SELECT protect FROM configprotectmask,configprotectreference WHERE idpackage = (?) and configprotectmask.idprotect= configprotectreference.idprotect', (idpackage,))
        protect = self.cursor.fetchone()
        if not protect:
            protect = ''
        else:
            protect = protect[0]
        return protect

    def retrieveSources(self, idpackage):
        self.cursor.execute('SELECT sourcesreference.source FROM sources,sourcesreference WHERE idpackage = (?) and sources.idsource = sourcesreference.idsource', (idpackage,))
        return self.fetchall2set(self.cursor.fetchall())

    def retrieveContent(self, idpackage, extended = False, contentType = None, formatted = False, insert_formatted = False):

        # like portage does
        self.connection.text_factory = lambda x: unicode(x, "raw_unicode_escape")

        extstring = ''
        if extended:
            extstring = ",type"
        extstring_idpackage = ''
        if insert_formatted:
            extstring_idpackage = 'idpackage,'

        searchkeywords = [idpackage]
        contentstring = ''
        if contentType:
            searchkeywords.append(contentType)
            contentstring = ' and type = (?)'

        self.cursor.execute('SELECT '+extstring_idpackage+'file'+extstring+' FROM content WHERE idpackage = (?) '+contentstring, searchkeywords)

        if extended and insert_formatted:
            fl = self.cursor.fetchall()
        elif extended and formatted:
            fl = {}
            items = self.cursor.fetchone()
            while items:
                fl[items[0]] = items[1]
                items = self.cursor.fetchone()
        elif extended:
            fl = self.cursor.fetchall()
        else:
            fl = self.fetchall2set(self.cursor.fetchall())

        return fl

    def retrieveSlot(self, idpackage):
        self.cursor.execute('SELECT slot FROM baseinfo WHERE idpackage = (?)', (idpackage,))
        slot = self.cursor.fetchone()
        if slot:
            return slot[0]

    def retrieveVersionTag(self, idpackage):
        self.cursor.execute('SELECT versiontag FROM baseinfo WHERE idpackage = (?)', (idpackage,))
        vtag = self.cursor.fetchone()
        if vtag:
            return vtag[0]

    def retrieveMirrorInfo(self, mirrorname):
        self.cursor.execute('SELECT mirrorlink FROM mirrorlinks WHERE mirrorname = (?)', (mirrorname,))
        mirrorlist = self.fetchall2set(self.cursor.fetchall())
        return mirrorlist

    def retrieveCategory(self, idpackage):
        self.cursor.execute('SELECT category FROM baseinfo,categories WHERE baseinfo.idpackage = (?) and baseinfo.idcategory = categories.idcategory', (idpackage,))
        cat = self.cursor.fetchone()
        if cat:
            return cat[0]

    def retrieveCategoryDescription(self, category):
        data = {}
        if not self.doesTableExist("categoriesdescription"):
            return data
        #self.connection.text_factory = lambda x: unicode(x, "raw_unicode_escape")
        self.cursor.execute('SELECT description,locale FROM categoriesdescription WHERE category = (?)', (category,))
        description_data = self.cursor.fetchall()
        for description,locale in description_data:
            data[locale] = description
        return data

    def retrieveLicensedata(self, idpackage):

        # insert license information
        if not self.doesTableExist("licensedata"):
            return {}
        licenses = self.retrieveLicense(idpackage)
        licenses = licenses.split()
        licdata = {}
        for licname in licenses:
            licname = licname.strip()
            if not self.entropyTools.is_valid_string(licname):
                continue

            self.connection.text_factory = lambda x: unicode(x, "raw_unicode_escape")

            self.cursor.execute('SELECT text FROM licensedata WHERE licensename = (?)', (licname,))
            lictext = self.cursor.fetchone()
            if lictext != None:
                licdata[licname] = str(lictext[0])

        return licdata

    def retrieveLicensedataKeys(self, idpackage):

        if not self.doesTableExist("licensedata"):
            return set()
        licenses = self.retrieveLicense(idpackage)
        licenses = licenses.split()
        licdata = set()
        for licname in licenses:
            licname = licname.strip()
            if not self.entropyTools.is_valid_string(licname):
                continue
            self.cursor.execute('SELECT licensename FROM licensedata WHERE licensename = (?)', (licname,))
            licidentifier = self.cursor.fetchone()
            if licidentifier:
                licdata.add(licidentifier[0])

        return licdata

    def retrieveLicenseText(self, license_name):

        if not self.doesTableExist("licensedata"):
            return None

        self.connection.text_factory = lambda x: unicode(x, "raw_unicode_escape")

        self.cursor.execute('SELECT text FROM licensedata WHERE licensename = (?)', (license_name,))
        text = self.cursor.fetchone()
        if not text:
            return None
        return str(text[0])

    def retrieveLicense(self, idpackage):
        self.cursor.execute('SELECT license FROM baseinfo,licenses WHERE baseinfo.idpackage = (?) and baseinfo.idlicense = licenses.idlicense', (idpackage,))
        licname = self.cursor.fetchone()
        if licname:
            return licname[0]

    def retrieveCompileFlags(self, idpackage):
        self.cursor.execute('SELECT chost,cflags,cxxflags FROM flags,extrainfo WHERE extrainfo.idpackage = (?) and extrainfo.idflags = flags.idflags', (idpackage,))
        flags = self.cursor.fetchone()
        if not flags:
            flags = ("N/A","N/A","N/A")
        return flags

    def retrieveDepends(self, idpackage, atoms = False, key_slot = False):

        # sanity check on the table
        if not self.isDependsTableSane(): # is empty, need generation
            self.regenerateDependsTable(output = False)

        if atoms:
            self.cursor.execute('SELECT baseinfo.atom FROM dependstable,dependencies,baseinfo WHERE dependstable.idpackage = (?) and dependstable.iddependency = dependencies.iddependency and baseinfo.idpackage = dependencies.idpackage', (idpackage,))
            result = self.fetchall2set(self.cursor.fetchall())
        elif key_slot:
            self.cursor.execute('SELECT categories.category || "/" || baseinfo.name,baseinfo.slot FROM baseinfo,categories,dependstable,dependencies WHERE dependstable.idpackage = (?) and dependstable.iddependency = dependencies.iddependency and baseinfo.idpackage = dependencies.idpackage and categories.idcategory = baseinfo.idcategory', (idpackage,))
            result = self.cursor.fetchall()
        else:
            self.cursor.execute('SELECT dependencies.idpackage FROM dependstable,dependencies WHERE dependstable.idpackage = (?) and dependstable.iddependency = dependencies.iddependency', (idpackage,))
            result = self.fetchall2set(self.cursor.fetchall())

        return result

    # You must provide the full atom to this function
    # WARNING: this function does not support branches
    # NOTE: server side uses this regardless branch specification because it already handles it in updatePackage()
    def isPackageAvailable(self,pkgatom):
        pkgatom = self.entropyTools.removePackageOperators(pkgatom)
        self.cursor.execute('SELECT idpackage FROM baseinfo WHERE atom = (?)', (pkgatom,))
        result = self.cursor.fetchone()
        if result:
            return result[0]
        return -1

    def isIDPackageAvailable(self,idpackage):
        self.cursor.execute('SELECT idpackage FROM baseinfo WHERE idpackage = (?)', (idpackage,))
        result = self.cursor.fetchone()
        if not result:
            return False
        return True

    # This version is more specific and supports branches
    def isSpecificPackageAvailable(self, pkgkey, branch, branch_operator = "="):
        pkgkey = self.entropyTools.removePackageOperators(pkgkey)
        self.cursor.execute('SELECT idpackage FROM baseinfo WHERE atom = (?) AND branch '+branch_operator+' (?)', (pkgkey,branch,))
        result = self.cursor.fetchone()
        if not result:
            return False
        return True

    def isCategoryAvailable(self,category):
        self.cursor.execute('SELECT idcategory FROM categories WHERE category = (?)', (category,))
        result = self.cursor.fetchone()
        if not result:
            return -1
        return result[0]

    def isProtectAvailable(self,protect):
        self.cursor.execute('SELECT idprotect FROM configprotectreference WHERE protect = (?)', (protect,))
        result = self.cursor.fetchone()
        if not result:
            return -1
        return result[0]

    def isFileAvailable(self, myfile, get_id = False):
        self.cursor.execute('SELECT idpackage FROM content WHERE file = (?)', (myfile,))
        result = self.cursor.fetchall()
        if get_id:
            return self.fetchall2set(result)
        elif result:
            return True
        return False

    def resolveNeeded(self, needed, elfclass = -1):

        cache = self.fetchSearchCache(needed,'resolveNeeded')
        if cache != None: return cache

        ldpaths = self.entropyTools.collectLinkerPaths()
        mypaths = [os.path.join(x,needed) for x in ldpaths]

        query = """
        SELECT
                idpackage,file
        FROM
                content
        WHERE
                content.file IN (%s)
        """ % ( ('?,'*len(mypaths))[:-1], )

        self.cursor.execute(query,mypaths)
        results = self.cursor.fetchall()

        if elfclass == -1:
            mydata = set(results)
        else:
            mydata = set()
            for data in results:
                if not os.access(data[1],os.R_OK):
                    continue
                myclass = self.entropyTools.read_elf_class(data[1])
                if myclass == elfclass:
                    mydata.add(data)

        self.storeSearchCache(needed,'resolveNeeded',mydata)
        return mydata

    def isSourceAvailable(self,source):
        self.cursor.execute('SELECT idsource FROM sourcesreference WHERE source = (?)', (source,))
        result = self.cursor.fetchone()
        if not result:
            return -1
        return result[0]

    def isDependencyAvailable(self,dependency):
        self.cursor.execute('SELECT iddependency FROM dependenciesreference WHERE dependency = (?)', (dependency,))
        result = self.cursor.fetchone()
        if not result:
            return -1
        return result[0]

    def isKeywordAvailable(self,keyword):
        self.cursor.execute('SELECT idkeyword FROM keywordsreference WHERE keywordname = (?)', (keyword,))
        result = self.cursor.fetchone()
        if not result:
            return -1
        return result[0]

    def isUseflagAvailable(self,useflag):
        self.cursor.execute('SELECT idflag FROM useflagsreference WHERE flagname = (?)', (useflag,))
        result = self.cursor.fetchone()
        if not result:
            return -1
        return result[0]

    def isEclassAvailable(self,eclass):
        self.cursor.execute('SELECT idclass FROM eclassesreference WHERE classname = (?)', (eclass,))
        result = self.cursor.fetchone()
        if not result:
            return -1
        return result[0]

    def isNeededAvailable(self,needed):
        self.cursor.execute('SELECT idneeded FROM neededreference WHERE library = (?)', (needed,))
        result = self.cursor.fetchone()
        if not result:
            return -1
        return result[0]

    def isCounterAvailable(self, counter, branch = None, branch_operator = "="):
        result = False
        if not branch:
            branch = etpConst['branch']
        self.cursor.execute('SELECT counter FROM counters WHERE counter = (?) and branch '+branch_operator+' (?)', (counter,branch,))
        result = self.cursor.fetchone()
        if result:
            result = True
        return result

    def isCounterTrashed(self, counter):
        self.cursor.execute('SELECT counter FROM trashedcounters WHERE counter = (?)', (counter,))
        result = self.cursor.fetchone()
        if result:
            return True
        return False

    def getIDPackageFromCounter(self, counter, branch = None, branch_operator = "="):
        if not branch:
            branch = etpConst['branch']
        self.cursor.execute('SELECT idpackage FROM counters WHERE counter = (?) and branch '+branch_operator+' (?)', (counter,branch,))
        result = self.cursor.fetchone()
        if not result:
            return 0
        return result[0]

    def isLicensedataKeyAvailable(self, license_name):
        if not self.doesTableExist("licensedata"):
            return True
        self.cursor.execute('SELECT licensename FROM licensedata WHERE licensename = (?)', (license_name,))
        result = self.cursor.fetchone()
        if not result:
            return False
        return True

    def isLicenseAccepted(self, license_name):
        self.cursor.execute('SELECT licensename FROM licenses_accepted WHERE licensename = (?)', (license_name,))
        result = self.cursor.fetchone()
        if not result:
            return False
        return True

    def acceptLicense(self, license_name):
        if self.readOnly or (not self.entropyTools.is_user_in_entropy_group()):
            return
        if self.isLicenseAccepted(license_name):
            return
        self.cursor.execute('INSERT INTO licenses_accepted VALUES (?)', (license_name,))
        self.commitChanges()

    def isLicenseAvailable(self,pkglicense):
        if not self.entropyTools.is_valid_string(pkglicense):
            pkglicense = ' '
        self.cursor.execute('SELECT idlicense FROM licenses WHERE license = (?)', (pkglicense,))
        result = self.cursor.fetchone()
        if not result:
            return -1
        return result[0]

    def isSystemPackage(self,idpackage):
        self.cursor.execute('SELECT idpackage FROM systempackages WHERE idpackage = (?)', (idpackage,))
        result = self.cursor.fetchone()
        if result:
            return True
        return False

    def isInjected(self,idpackage):
        self.cursor.execute('SELECT idpackage FROM injected WHERE idpackage = (?)', (idpackage,))
        result = self.cursor.fetchone()
        if result:
            return True
        return False

    def areCompileFlagsAvailable(self,chost,cflags,cxxflags):

        self.cursor.execute('SELECT idflags FROM flags WHERE chost = (?) AND cflags = (?) AND cxxflags = (?)', 
            (chost,cflags,cxxflags,)
        )
        result = self.cursor.fetchone()
        if not result:
            return -1
        return result[0]

    def searchBelongs(self, file, like = False, branch = None, branch_operator = "="):

        branchstring = ''
        searchkeywords = [file]
        if branch:
            searchkeywords.append(branch)
            branchstring = ' and baseinfo.branch '+branch_operator+' (?)'

        if like:
            self.cursor.execute('SELECT content.idpackage FROM content,baseinfo WHERE file LIKE (?) and content.idpackage = baseinfo.idpackage '+branchstring, searchkeywords)
        else:
            self.cursor.execute('SELECT content.idpackage FROM content,baseinfo WHERE file = (?) and content.idpackage = baseinfo.idpackage '+branchstring, searchkeywords)

        return self.fetchall2set(self.cursor.fetchall())

    ''' search packages that uses the eclass provided '''
    def searchEclassedPackages(self, eclass, atoms = False): # atoms = return atoms directly
        if atoms:
            self.cursor.execute('SELECT baseinfo.atom,eclasses.idpackage FROM baseinfo,eclasses,eclassesreference WHERE eclassesreference.classname = (?) and eclassesreference.idclass = eclasses.idclass and eclasses.idpackage = baseinfo.idpackage', (eclass,))
            return self.cursor.fetchall()
        else:
            self.cursor.execute('SELECT idpackage FROM baseinfo WHERE versiontag = (?)', (eclass,))
            return self.fetchall2set(self.cursor.fetchall())

    ''' search packages whose versiontag matches the one provided '''
    def searchTaggedPackages(self, tag, atoms = False): # atoms = return atoms directly
        if atoms:
            self.cursor.execute('SELECT atom,idpackage FROM baseinfo WHERE versiontag = (?)', (tag,))
            return self.cursor.fetchall()
        else:
            self.cursor.execute('SELECT idpackage FROM baseinfo WHERE versiontag = (?)', (tag,))
            return self.fetchall2set(self.cursor.fetchall())

    def searchLicenses(self, mylicense, caseSensitive = False, atoms = False):

        if not self.entropyTools.is_valid_string(mylicense):
            return []

        request = "baseinfo.idpackage"
        if atoms:
            request = "baseinfo.atom,baseinfo.idpackage"

        if caseSensitive:
            self.cursor.execute('SELECT '+request+' FROM baseinfo,licenses WHERE licenses.license LIKE (?) and licenses.idlicense = baseinfo.idlicense', ("%"+mylicense+"%",))
        else:
            self.cursor.execute('SELECT '+request+' FROM baseinfo,licenses WHERE LOWER(licenses.license) LIKE (?) and licenses.idlicense = baseinfo.idlicense', ("%"+mylicense+"%".lower(),))
        if atoms:
            return self.cursor.fetchall()
        return self.fetchall2set(self.cursor.fetchall())

    ''' search packages whose slot matches the one provided '''
    def searchSlottedPackages(self, slot, atoms = False): # atoms = return atoms directly
        if atoms:
            self.cursor.execute('SELECT atom,idpackage FROM baseinfo WHERE slot = (?)', (slot,))
            return self.cursor.fetchall()
        else:
            self.cursor.execute('SELECT idpackage FROM baseinfo WHERE slot = (?)', (slot,))
            return self.fetchall2set(self.cursor.fetchall())

    def searchKeySlot(self, key, slot, branch = None):

        branchstring = ''
        params = [key,slot]
        if branch:
            params.append(branch)
            branchstring = ' and baseinfo.branch = (?)'

        self.cursor.execute('SELECT idpackage FROM baseinfo,categories WHERE categories.category || "/" || baseinfo.name = (?) and baseinfo.slot = (?) and baseinfo.idcategory = categories.idcategory'+branchstring, params)
        data = self.cursor.fetchall()

        return data

    ''' search packages that need the specified library (in neededreference table) specified by keyword '''
    def searchNeeded(self, keyword, like = False):
        if like:
            self.cursor.execute('SELECT needed.idpackage FROM needed,neededreference WHERE library LIKE (?) and needed.idneeded = neededreference.idneeded', (keyword,))
        else:
            self.cursor.execute('SELECT needed.idpackage FROM needed,neededreference WHERE library = (?) and needed.idneeded = neededreference.idneeded', (keyword,))
	return self.fetchall2set(self.cursor.fetchall())

    # FIXME: deprecate and add functionalities to the function above
    ''' same as above but with branch support '''
    def searchNeededInBranch(self, keyword, branch):
	self.cursor.execute('SELECT needed.idpackage FROM needed,neededreference,baseinfo WHERE library = (?) and needed.idneeded = neededreference.idneeded and baseinfo.branch = (?)', (keyword,branch,))
	return self.fetchall2set(self.cursor.fetchall())

    ''' search dependency string inside dependenciesreference table and retrieve iddependency '''
    def searchDependency(self, dep, like = False, multi = False, strings = False):
        sign = "="
        if like:
            sign = "LIKE"
            dep = "%"+dep+"%"
        item = 'iddependency'
        if strings:
            item = 'dependency'
        self.cursor.execute('SELECT '+item+' FROM dependenciesreference WHERE dependency '+sign+' (?)', (dep,))
        if multi:
            return self.fetchall2set(self.cursor.fetchall())
        else:
            iddep = self.cursor.fetchone()
            if iddep:
                iddep = iddep[0]
            else:
                iddep = -1
            return iddep

    ''' search iddependency inside dependencies table and retrieve idpackages '''
    def searchIdpackageFromIddependency(self, iddep):
        self.cursor.execute('SELECT idpackage FROM dependencies WHERE iddependency = (?)', (iddep,))
        return self.fetchall2set(self.cursor.fetchall())

    def searchPackages(self, keyword, sensitive = False, slot = None, tag = None, branch = None):

        searchkeywords = ["%"+keyword+"%"]
        slotstring = ''
        if slot:
            searchkeywords.append(slot)
            slotstring = ' and slot = (?)'
        tagstring = ''
        if tag:
            searchkeywords.append(tag)
            tagstring = ' and versiontag = (?)'
        branchstring = ''
        if branch:
            searchkeywords.append(branch)
            branchstring = ' and branch = (?)'

        if (sensitive):
            self.cursor.execute('SELECT atom,idpackage,branch FROM baseinfo WHERE atom LIKE (?)'+slotstring+tagstring+branchstring, searchkeywords)
        else:
            self.cursor.execute('SELECT atom,idpackage,branch FROM baseinfo WHERE LOWER(atom) LIKE (?)'+slotstring+tagstring+branchstring, searchkeywords)
        return self.cursor.fetchall()

    def searchProvide(self, keyword, slot = None, tag = None, branch = None, justid = False):

        slotstring = ''
        searchkeywords = [keyword]
        if slot:
            searchkeywords.append(slot)
            slotstring = ' and baseinfo.slot = (?)'
        tagstring = ''
        if tag:
            searchkeywords.append(tag)
            tagstring = ' and baseinfo.versiontag = (?)'
        branchstring = ''
        if branch:
            searchkeywords.append(branch)
            branchstring = ' and baseinfo.branch = (?)'
        atomstring = ''
        if not justid:
            atomstring = 'baseinfo.atom,'

        self.cursor.execute('SELECT '+atomstring+'baseinfo.idpackage FROM baseinfo,provide WHERE provide.atom = (?) and provide.idpackage = baseinfo.idpackage'+slotstring+tagstring+branchstring, searchkeywords)

        if justid:
            results = self.fetchall2list(self.cursor.fetchall())
        else:
            results = self.cursor.fetchall()
        return results

    def searchPackagesByDescription(self, keyword):
        self.cursor.execute('SELECT baseinfo.atom,baseinfo.idpackage FROM extrainfo,baseinfo WHERE LOWER(extrainfo.description) LIKE (?) and baseinfo.idpackage = extrainfo.idpackage', ("%"+keyword.lower()+"%",))
        return self.cursor.fetchall()

    def searchPackagesByName(self, keyword, sensitive = False, branch = None, justid = False):

        if sensitive:
            searchkeywords = [keyword]
        else:
            searchkeywords = [keyword.lower()]
        branchstring = ''
        atomstring = ''
        if not justid:
            atomstring = 'atom,'
        if branch:
            searchkeywords.append(branch)
            branchstring = ' and branch = (?)'

        if sensitive:
            self.cursor.execute('SELECT '+atomstring+'idpackage FROM baseinfo WHERE name = (?)'+branchstring, searchkeywords)
        else:
            self.cursor.execute('SELECT '+atomstring+'idpackage FROM baseinfo WHERE LOWER(name) = (?)'+branchstring, searchkeywords)

        if justid:
            results = self.fetchall2list(self.cursor.fetchall())
        else:
            results = self.cursor.fetchall()
        return results


    def searchPackagesByCategory(self, keyword, like = False, branch = None):

        searchkeywords = [keyword]
        branchstring = ''
        if branch:
            searchkeywords.append(branch)
            branchstring = ' and branch = (?)'

        if like:
            self.cursor.execute('SELECT baseinfo.atom,baseinfo.idpackage FROM baseinfo,categories WHERE categories.category LIKE (?) and baseinfo.idcategory = categories.idcategory '+branchstring, searchkeywords)
        else:
            self.cursor.execute('SELECT baseinfo.atom,baseinfo.idpackage FROM baseinfo,categories WHERE categories.category = (?) and baseinfo.idcategory = categories.idcategory '+branchstring, searchkeywords)

        results = self.cursor.fetchall()

        return results

    def searchPackagesByNameAndCategory(self, name, category, sensitive = False, branch = None, justid = False):

        myname = name
        mycat = category
        if not sensitive:
            myname = name.lower()
            mycat = category.lower()

        searchkeywords = [myname,mycat]
        branchstring = ''
        if branch:
            searchkeywords.append(branch)
            branchstring = ' and branch = (?)'
        atomstring = ''
        if not justid:
            atomstring = 'atom,'

        if sensitive:
            self.cursor.execute('SELECT '+atomstring+'idpackage FROM baseinfo WHERE name = (?) AND idcategory IN (SELECT idcategory FROM categories WHERE category = (?))'+branchstring, searchkeywords)
        else:
            self.cursor.execute('SELECT '+atomstring+'idpackage FROM baseinfo WHERE LOWER(name) = (?) AND idcategory IN (SELECT idcategory FROM categories WHERE LOWER(category) = (?))'+branchstring, searchkeywords)
            ''

        if justid:
            results = self.fetchall2list(self.cursor.fetchall())
        else:
            results = self.cursor.fetchall()
        return results

    def searchPackagesKeyVersion(self, key, version, branch = None, sensitive = False):

        searchkeywords = []
        if sensitive:
            searchkeywords.append(key)
        else:
            searchkeywords.append(key.lower())

        searchkeywords.append(version)

        branchstring = ''
        if branch:
            searchkeywords.append(branch)
            branchstring = ' and branch = (?)'

        if (sensitive):
            self.cursor.execute('SELECT baseinfo.atom,baseinfo.idpackage FROM baseinfo,categories WHERE categories.category || "/" || baseinfo.name = (?) and version = (?) and baseinfo.idcategory = categories.idcategory '+branchstring, searchkeywords)
        else:
            self.cursor.execute('SELECT baseinfo.atom,baseinfo.idpackage FROM baseinfo,categories WHERE LOWER(categories.category) || "/" || LOWER(baseinfo.name) = (?) and version = (?) and baseinfo.idcategory = categories.idcategory '+branchstring, searchkeywords)

        results = self.cursor.fetchall()

        return results

    def listAllPackages(self):
        self.cursor.execute('SELECT atom,idpackage,branch FROM baseinfo')
        return self.cursor.fetchall()

    def listAllInjectedPackages(self, justFiles = False):
        self.cursor.execute('SELECT idpackage FROM injected')
        injecteds = self.fetchall2set(self.cursor.fetchall())
        results = set()
        # get download
        for injected in injecteds:
            download = self.retrieveDownloadURL(injected)
            if justFiles:
                results.add(download)
            else:
                results.add((download,injected))
        return results

    def listAllCounters(self, onlycounters = False, branch = None, branch_operator = "="):

        branchstring = ''
        if branch:
            branchstring = ' WHERE branch '+branch_operator+' "'+str(branch)+'"'
        if onlycounters:
            self.cursor.execute('SELECT counter FROM counters'+branchstring)
            return self.fetchall2set(self.cursor.fetchall())
        else:
            self.cursor.execute('SELECT counter,idpackage FROM counters'+branchstring)
            return self.cursor.fetchall()

    def listAllIdpackages(self, branch = None, branch_operator = "=", order_by = None):

        branchstring = ''
        orderbystring = ''
        searchkeywords = []
        if branch:
            searchkeywords.append(branch)
            branchstring = ' where branch %s (?)' % (str(branch_operator),)
        if order_by:
            orderbystring = ' order by '+order_by

        self.cursor.execute('SELECT idpackage FROM baseinfo'+branchstring+orderbystring, searchkeywords)

        if order_by:
            results = self.fetchall2list(self.cursor.fetchall())
        else:
            results = self.fetchall2set(self.cursor.fetchall())
        return results

    def listAllDependencies(self, only_deps = False):
        if only_deps:
            self.cursor.execute('SELECT dependency FROM dependenciesreference')
            return self.fetchall2set(self.cursor.fetchall())
        else:
            self.cursor.execute('SELECT * FROM dependenciesreference')
            return self.cursor.fetchall()

    def listAllBranches(self):

        cache = self.live_cache.get('listAllBranches')
        if cache != None:
            return cache

        self.cursor.execute('SELECT distinct branch FROM baseinfo')
        results = self.fetchall2set(self.cursor.fetchall())

        self.live_cache['listAllBranches'] = results.copy()
        return results

    def listIdPackagesInIdcategory(self,idcategory):
        self.cursor.execute('SELECT idpackage FROM baseinfo where idcategory = (?)', (idcategory,))
        return self.fetchall2set(self.cursor.fetchall())

    def listIdpackageDependencies(self, idpackage):
        self.cursor.execute('SELECT dependenciesreference.iddependency,dependenciesreference.dependency FROM dependenciesreference,dependencies WHERE dependencies.idpackage = (?) AND dependenciesreference.iddependency = dependencies.iddependency', (idpackage,))
        return set(self.cursor.fetchall())

    def listBranchPackagesTbz2(self, branch, do_sort = True):
        self.cursor.execute('SELECT extrainfo.download FROM baseinfo,extrainfo WHERE baseinfo.branch = (?) AND baseinfo.idpackage = extrainfo.idpackage', (branch,))
        result = self.fetchall2set(self.cursor.fetchall())
        sorted_result = []
        for package in result:
            if package:
                sorted_result.append(os.path.basename(package))
        if do_sort:
            sorted_result.sort()
        return sorted_result

    def listBranchPackages(self, branch):
        self.cursor.execute('SELECT atom,idpackage FROM baseinfo WHERE branch = (?)', (branch,))
        return self.cursor.fetchall()

    def listAllFiles(self, clean = False):
        self.cursor.execute('SELECT file FROM content')
        if clean:
            return self.fetchall2set(self.cursor.fetchall())
        else:
            return self.fetchall2list(self.cursor.fetchall())

    def listAllCategories(self):
        self.cursor.execute('SELECT idcategory,category FROM categories')
        return self.cursor.fetchall()

    def listConfigProtectDirectories(self, mask = False):
        query = 'SELECT max(idprotect) FROM configprotect'
        if mask:
            query += 'mask'
        self.cursor.execute(query)
        r = self.cursor.fetchone()
        if not r:
            return []

        mymax = r[0]
        self.cursor.execute('SELECT protect FROM configprotectreference where idprotect >= (?) and idprotect <= (?) order by protect', (1,mymax,))
        results = self.cursor.fetchall()
        dirs = []
        for row in results:
            mydirs = row[0].split()
            for x in mydirs:
                if x not in dirs:
                    dirs.append(x)
        return dirs

    def switchBranch(self, idpackage, tobranch):
        self.checkReadOnly()

        mycat = self.retrieveCategory(idpackage)
        myname = self.retrieveName(idpackage)
        myslot = self.retrieveSlot(idpackage)
        mybranch = self.retrieveBranch(idpackage)
        mydownload = self.retrieveDownloadURL(idpackage)
        import re
        out = re.subn('/'+mybranch+'/','/'+tobranch+'/',mydownload)
        newdownload = out[0]

        # remove package with the same key+slot and tobranch if exists
        match = self.atomMatch(mycat+"/"+myname, matchSlot = myslot, matchBranches = (tobranch,))
        if match[0] != -1:
            self.removePackage(match[0])

        # now switch selected idpackage to the new branch
        self.cursor.execute('UPDATE baseinfo SET branch = (?) WHERE idpackage = (?)', (tobranch,idpackage,))
        self.cursor.execute('UPDATE extrainfo SET download = (?) WHERE idpackage = (?)', (newdownload,idpackage,))
        self.commitChanges()

    def databaseStructureUpdates(self):

        if not self.doesTableExist("licensedata"):
            self.createLicensedataTable()

        if not self.doesTableExist("licenses_accepted") and (self.dbname == etpConst['clientdbid']):
            self.createLicensesAcceptedTable()

        if not self.doesColumnInTableExist("baseinfo","trigger"):
            self.createTriggerColumn()

        if not self.doesTableExist("counters"):
            self.createCountersTable()
        elif not self.doesColumnInTableExist("counters","branch"):
            self.createCountersBranchColumn()

        if not self.doesTableExist("trashedcounters"):
            self.createTrashedcountersTable()

        if not self.doesTableExist("sizes"):
            self.createSizesTable()

        if not self.doesTableExist("triggers"):
            self.createTriggerTable()

        if not self.doesTableExist("messages"):
            self.createMessagesTable()

        if not self.doesTableExist("injected"):
            self.createInjectedTable()

        if not self.doesTableExist("systempackages"):
            self.createSystemPackagesTable()

        if (not self.doesTableExist("configprotect")) or (not self.doesTableExist("configprotectreference")):
            self.createProtectTable()

        if not self.doesColumnInTableExist("content","type"):
            self.createContentTypeColumn()

        if not self.doesTableExist("eclasses"):
            self.createEclassesTable()

        if not self.doesTableExist("treeupdates"):
            self.createTreeupdatesTable()

        if not self.doesTableExist("treeupdatesactions"):
            self.createTreeupdatesactionsTable()
        elif not self.doesColumnInTableExist("treeupdatesactions","branch"):
            self.createTreeupdatesactionsBranchColumn()
        elif not self.doesColumnInTableExist("treeupdatesactions","date"):
            self.createTreeupdatesactionsDateColumn()

        if not self.doesTableExist("needed"):
            self.createNeededTable()
        elif not self.doesColumnInTableExist("needed","elfclass"):
            self.createNeededElfclassColumn()

        if not self.doesTableExist("installedtable") and (self.dbname == etpConst['clientdbid']):
            self.createInstalledTable()

        if not self.doesTableExist("entropy_misc_counters"):
            self.createEntropyMiscCountersTable()

        if not self.doesColumnInTableExist("dependencies","type"):
            self.createDependenciesTypeColumn()

        if not self.doesTableExist("categoriesdescription"):
            self.createCategoriesdescriptionTable()

        # these are the tables moved to INTEGER PRIMARY KEY AUTOINCREMENT
        autoincrement_tables = [
            'treeupdatesactions',
            'neededreference',
            'eclassesreference',
            'configprotectreference',
            'flags',
            'licenses',
            'categories',
            'keywordsreference',
            'useflagsreference',
            'sourcesreference',
            'dependenciesreference',
            'baseinfo'
        ]
        autoinc = False
        for table in autoincrement_tables:
            x = self.migrateTableToAutoincrement(table)
            if x: autoinc = True
        if autoinc:
            mytxt = red("%s: %s.") % (_("Entropy database"),_("regenerating indexes after migration"),)
            self.updateProgress(
                mytxt,
                importance = 1,
                type = "warning",
                header = blue(" !!! ")
            )
            self.createAllIndexes()

        # do manual atoms update
        # FIXME: remove this ASAP (0.16.x branch)
        if os.access(self.dbFile,os.W_OK) and \
            (self.dbname != etpConst['genericdbid']):
                old_readonly = self.readOnly
                self.readOnly = False
                self.fixKdeDepStrings()
                self.readOnly = old_readonly

        self.connection.commit()

    def migrateTableToAutoincrement(self, table):

        self.cursor.execute('select sql from sqlite_master where type = (?) and name = (?);', ("table",table))
        schema = self.cursor.fetchone()
        if not schema:
            return False
        schema = schema[0]
        if schema.find("AUTOINCREMENT") != -1:
            return False
        schema = schema.replace('PRIMARY KEY','PRIMARY KEY AUTOINCREMENT')
        new_schema = schema
        totable = table+"_autoincrement"
        schema = schema.replace('CREATE TABLE '+table,'CREATE TEMPORARY TABLE '+totable)
        mytxt = "%s: %s %s" % (red(_("Entropy database")),red(_("migrating table")),blue(table),)
        self.updateProgress(
            mytxt,
            importance = 1,
            type = "warning",
            header = blue(" !!! ")
        )
        # create table
        self.cursor.execute('DROP TABLE IF EXISTS '+totable)
        self.cursor.execute(schema)
        columns = ','.join(self.getColumnsInTable(table))

        temp_query = 'INSERT INTO '+totable+' SELECT '+columns+' FROM '+table
        self.cursor.execute(temp_query)

        self.cursor.execute('DROP TABLE '+table)
        self.cursor.execute(new_schema)

        temp_query = 'INSERT INTO '+table+' SELECT '+columns+' FROM '+totable
        self.cursor.execute(temp_query)

        self.cursor.execute('DROP TABLE '+totable)
        self.commitChanges()
        return True

    def fixKdeDepStrings(self):

        # check if we need to do it
        cur_id = self.getForcedAtomsUpdateId()
        if cur_id >= etpConst['misc_counters']['forced_atoms_update_ids']['kde']:
            return

        mytxt = "%s: %s %s. %s %s" % (
            red(_("Entropy database")),
            red(_("fixing KDE dep strings on")),
            blue(self.dbname),
            red(_("Please wait")),
            red("..."),
        )
        self.updateProgress(
            mytxt,
            importance = 1,
            type = "warning",
            header = blue(" !!! ")
        )

        # uhu, let's roooock
        search_deps = {
            ">=kde-base/kdelibs-3.0": 'kde-base/kdelibs:3.5',
            ">=kde-base/kdelibs-3.1": 'kde-base/kdelibs:3.5',
            ">=kde-base/kdelibs-3.2": 'kde-base/kdelibs:3.5',
            ">=kde-base/kdelibs-3.3": 'kde-base/kdelibs:3.5',
            ">=kde-base/kdelibs-3.4": 'kde-base/kdelibs:3.5',
            ">=kde-base/kdelibs-3.5": 'kde-base/kdelibs:3.5',
            ">=kde-base/kdelibs-3": 'kde-base/kdelibs:3.5',
            ">=kde-base/kdelibs-3.0": 'kde-base/kdelibs:3.5',
            ">=kde-base/kdelibs-3.0.0": 'kde-base/kdelibs:3.5',
            ">=kde-base/kdelibs-3.0.5": 'kde-base/kdelibs:3.5',
            ">=kde-base/kdelibs-3.1": 'kde-base/kdelibs:3.5',
            ">=kde-base/kdelibs-3.1.0": 'kde-base/kdelibs:3.5',

        }
        self.cursor.execute('select iddependency,dependency from dependenciesreference')
        depdata = self.cursor.fetchall()
        for iddepedency, depstring in depdata:
            if depstring in search_deps:
                self.setDependency(iddepedency, search_deps[depstring])

        # regenerate depends
        while 1: # avoid users interruption
            self.regenerateDependsTable()
            break

        self.setForcedAtomsUpdateId(etpConst['misc_counters']['forced_atoms_update_ids']['kde'])
        self.commitChanges()
        # drop all cache
        self.clearCache(depends = True)


    def getForcedAtomsUpdateId(self):
        self.cursor.execute(
            'SELECT counter FROM entropy_misc_counters WHERE idtype = (?)',
            (etpConst['misc_counters']['forced_atoms_update_ids']['__idtype__'],)
        )
        myid = self.cursor.fetchone()
        if not myid:
            return self.setForcedAtomsUpdateId(0)
        return myid[0]

    def setForcedAtomsUpdateId(self, myid):
        self.cursor.execute(
            'DELETE FROM entropy_misc_counters WHERE idtype = (?)',
            (etpConst['misc_counters']['forced_atoms_update_ids']['__idtype__'],)
        )
        self.cursor.execute(
            'INSERT INTO entropy_misc_counters VALUES (?,?)',
            (etpConst['misc_counters']['forced_atoms_update_ids']['__idtype__'],myid)
        )
        return myid

    def validateDatabase(self):
        self.cursor.execute('select name from SQLITE_MASTER where type = (?) and name = (?)', ("table","baseinfo"))
        rslt = self.cursor.fetchone()
        if rslt == None:
            mytxt = _("baseinfo table not found. Either does not exist or corrupted.")
            raise exceptionTools.SystemDatabaseError("SystemDatabaseError: %s" % (mytxt,))
        self.cursor.execute('select name from SQLITE_MASTER where type = (?) and name = (?)', ("table","extrainfo"))
        rslt = self.cursor.fetchone()
        if rslt == None:
            mytxt = _("extrainfo table not found. Either does not exist or corrupted.")
            raise exceptionTools.SystemDatabaseError("SystemDatabaseError: %s" % (mytxt,))

    def getIdpackagesDifferences(self, foreign_idpackages):
        myids = self.listAllIdpackages()
        if type(foreign_idpackages) in (list,tuple,):
            outids = set(foreign_idpackages)
        else:
            outids = foreign_idpackages
        added_ids = outids - myids
        removed_ids = myids - outids
        return added_ids, removed_ids

    def alignDatabases(self, dbconn, force = False, output_header = "  ", align_limit = 300):

        added_ids, removed_ids = self.getIdpackagesDifferences(dbconn.listAllIdpackages())

        if not force:
            if len(added_ids) > align_limit: # too much hassle
                return 0
            if len(removed_ids) > align_limit: # too much hassle
                return 0

        if not added_ids and not removed_ids:
            return -1

        mytxt = red("%s, %s ...") % (_("Syncing current database"),_("please wait"),)
        self.updateProgress(
            mytxt,
            importance = 1,
            type = "info",
            header = output_header,
            back = True
        )
        maxcount = len(removed_ids)
        mycount = 0
        for idpackage in removed_ids:
            mycount += 1
            mytxt = "%s: %s" % (red(_("Removing entry")),blue(str(self.retrieveAtom(idpackage))),)
            self.updateProgress(
                mytxt,
                importance = 0,
                type = "info",
                header = output_header,
                back = True,
                count = (mycount,maxcount)
            )
            self.removePackage(idpackage, do_cleanup = False, do_commit = False)

        maxcount = len(added_ids)
        mycount = 0
        for idpackage in added_ids:
            mycount += 1
            mytxt = "%s: %s" % (red(_("Adding entry")),blue(str(dbconn.retrieveAtom(idpackage))),)
            self.updateProgress(
                mytxt,
                importance = 0,
                type = "info",
                header = output_header,
                back = True,
                count = (mycount,maxcount)
            )
            mydata = dbconn.getPackageData(idpackage, get_content = True, content_insert_formatted = True)
            self.addPackage(
                mydata,
                revision = mydata['revision'],
                idpackage = idpackage,
                do_remove = False,
                do_commit = False,
                formatted_content = True
            )

        # do some cleanups
        self.doCleanups()
        # clear caches
        self.clearCache()
        self.commitChanges()
        self.regenerateDependsTable(output = False)

        # verify both checksums, if they don't match, bomb out
        mycheck = self.database_checksum(do_order = True, strict = False)
        outcheck = dbconn.database_checksum(do_order = True, strict = False)
        if mycheck == outcheck:
            return 1
        return 0

    def checkDatabaseApi(self):

        dbapi = self.getApi()
        if int(dbapi) > int(etpConst['etpapi']):
            self.updateProgress(
                red(_("Repository EAPI > Entropy EAPI. Please update Equo/Entropy as soon as possible !")),
                importance = 1,
                type = "warning",
                header = " * ! * ! * ! * "
            )

    def doDatabaseImport(self, dumpfile, dbfile):
        import subprocess
        sqlite3_exec = "/usr/bin/sqlite3 %s < %s" % (dbfile,dumpfile,)
        retcode = subprocess.call(sqlite3_exec, shell = True)
        return retcode

    def doDatabaseExport(self, dumpfile, gentle_with_tables = True):

        dumpfile.write("BEGIN TRANSACTION;\n")
        self.cursor.execute("SELECT name, type, sql FROM sqlite_master WHERE sql NOT NULL AND type=='table'")
        for name, x, sql in self.cursor.fetchall():

            self.updateProgress(
                red("%s " % (_("Exporting database table"),) )+"["+blue(str(name))+"]",
                importance = 0,
                type = "info",
                back = True,
                header = "   "
            )

            if name == "sqlite_sequence":
                dumpfile.write("DELETE FROM sqlite_sequence;\n")
            elif name == "sqlite_stat1":
                dumpfile.write("ANALYZE sqlite_master;\n")
            elif name.startswith("sqlite_"):
                continue
            else:
                t_cmd = "CREATE TABLE"
                if sql.startswith(t_cmd) and gentle_with_tables:
                    sql = "CREATE TABLE IF NOT EXISTS"+sql[len(t_cmd):]
                dumpfile.write("%s;\n" % sql)

            self.cursor.execute("PRAGMA table_info('%s')" % name)
            cols = [str(r[1]) for r in self.cursor.fetchall()]
            q = "SELECT 'INSERT INTO \"%(tbl_name)s\" VALUES("
            q += ", ".join(["'||quote(" + x + ")||'" for x in cols])
            q += ")' FROM '%(tbl_name)s'"
            self.cursor.execute(q % {'tbl_name': name})
            self.connection.text_factory = lambda x: unicode(x, "raw_unicode_escape")
            for row in self.cursor:
                dumpfile.write("%s;\n" % str(row[0].encode('raw_unicode_escape')))

        self.cursor.execute("SELECT name, type, sql FROM sqlite_master WHERE sql NOT NULL AND type!='table' AND type!='meta'")
        for name, x, sql in self.cursor.fetchall():
            dumpfile.write("%s;\n" % sql)

        dumpfile.write("COMMIT;\n")
        try:
            dumpfile.flush()
        except:
            pass
        self.updateProgress(
            red(_("Database Export completed.")),
            importance = 0,
            type = "info",
            header = "   "
        )
        # remember to close the file


    # FIXME: this is only compatible with SQLITE
    def doesTableExist(self, table):
        self.cursor.execute('select name from SQLITE_MASTER where type = (?) and name = (?)', ("table",table))
        rslt = self.cursor.fetchone()
        if rslt == None:
            return False
        return True

    # FIXME: this is only compatible with SQLITE
    def doesColumnInTableExist(self, table, column):
        self.cursor.execute('PRAGMA table_info( '+table+' )')
        rslt = self.cursor.fetchall()
        if not rslt:
            return False
        found = False
        for row in rslt:
            if row[1] == column:
                found = True
                break
        return found

    # FIXME: this is only compatible with SQLITE
    def getColumnsInTable(self, table):
        self.cursor.execute('PRAGMA table_info( '+table+' )')
        rslt = self.cursor.fetchall()
        columns = []
        for row in rslt:
            columns.append(row[1])
        return columns

    def database_checksum(self, do_order = False, strict = True):
        # primary keys are now autoincrement
        idpackage_order = ''
        category_order = ''
        license_order = ''
        flags_order = ''
        if do_order:
            idpackage_order = ' order by idpackage'
            category_order = ' order by category'
            license_order = ' order by license'
            flags_order = ' order by chost'

        self.cursor.execute('select idpackage,atom,name,version,versiontag,revision,branch,slot,etpapi,trigger from baseinfo'+idpackage_order)
        a_hash = hash(tuple(self.cursor.fetchall()))
        self.cursor.execute('select idpackage,description,homepage,download,size,digest,datecreation from extrainfo'+idpackage_order)
        b_hash = hash(tuple(self.cursor.fetchall()))
        self.cursor.execute('select category from categories'+category_order)
        c_hash = hash(tuple(self.cursor.fetchall()))
        d_hash = '0'
        e_hash = '0'
        if strict:
            self.cursor.execute('select * from licenses'+license_order)
            d_hash = hash(tuple(self.cursor.fetchall()))
            self.cursor.execute('select * from flags'+flags_order)
            e_hash = hash(tuple(self.cursor.fetchall()))
        return str(a_hash)+":"+str(b_hash)+":"+str(c_hash)+":"+str(d_hash)+":"+str(e_hash)


########################################################
####
##   Client Database API / but also used by server part
#

    def addPackageToInstalledTable(self, idpackage, repositoryName):
        self.checkReadOnly()
        self.cursor.execute(
                'INSERT into installedtable VALUES '
                '(?,?)'
                , (	idpackage,
                        repositoryName,
                        )
        )
        self.commitChanges()

    def retrievePackageFromInstalledTable(self, idpackage):
        self.checkReadOnly()
        result = 'Not available'
        try:
            self.cursor.execute('SELECT repositoryname FROM installedtable WHERE idpackage = (?)', (idpackage,))
            return self.cursor.fetchone()[0] # it's ok because it's inside try/except
        except:
            pass
        return result

    def removePackageFromInstalledTable(self, idpackage):
        self.cursor.execute('DELETE FROM installedtable WHERE idpackage = (?)', (idpackage,))

    def removePackageFromDependsTable(self, idpackage):
        try:
            self.cursor.execute('DELETE FROM dependstable WHERE idpackage = (?)', (idpackage,))
            return 0
        except:
            return 1 # need reinit

    def removeDependencyFromDependsTable(self, iddependency):
        self.checkReadOnly()
        try:
            self.cursor.execute('DELETE FROM dependstable WHERE iddependency = (?)',(iddependency,))
            self.commitChanges()
            return 0
        except:
            return 1 # need reinit

    # temporary/compat functions
    def createDependsTable(self):
        self.checkReadOnly()
        self.cursor.execute('DROP TABLE IF EXISTS dependstable;')
        self.cursor.execute('CREATE TABLE dependstable ( iddependency INTEGER PRIMARY KEY, idpackage INTEGER );')
        # this will be removed when dependstable is refilled properly
        self.cursor.execute(
                'INSERT into dependstable VALUES '
                '(?,?)'
                , (	-1,
                        -1,
                        )
        )
        if self.indexing:
            self.cursor.execute('CREATE INDEX IF NOT EXISTS dependsindex_idpackage ON dependstable ( idpackage )')
        self.commitChanges()

    def sanitizeDependsTable(self):
        self.cursor.execute('DELETE FROM dependstable where iddependency = -1')
        self.commitChanges()

    def isDependsTableSane(self):
        try:
            self.cursor.execute('SELECT iddependency FROM dependstable WHERE iddependency = -1')
        except:
            return False # table does not exist, please regenerate and re-run
        status = self.cursor.fetchone()
        if status:
            return False

        self.cursor.execute('select count(*) from dependstable')
        dependstable_count = self.cursor.fetchone()
        if dependstable_count == 0:
            return False
        return True

    def createXpakTable(self):
        self.checkReadOnly()
        self.cursor.execute('CREATE TABLE xpakdata ( idpackage INTEGER PRIMARY KEY, data BLOB );')
        self.commitChanges()

    def storeXpakMetadata(self, idpackage, blob):
        self.cursor.execute(
                'INSERT into xpakdata VALUES '
                '(?,?)', ( int(idpackage), buffer(blob), )
        )
        self.commitChanges()

    def retrieveXpakMetadata(self, idpackage):
        try:
            self.cursor.execute('SELECT data from xpakdata where idpackage = (?)', (idpackage,))
            mydata = self.cursor.fetchone()
            if not mydata:
                return ""
            else:
                return mydata[0]
        except:
            return ""
            pass

    def createCountersTable(self):
        self.cursor.execute('DROP TABLE IF EXISTS counters;')
        self.cursor.execute('CREATE TABLE counters ( counter INTEGER, idpackage INTEGER PRIMARY KEY, branch VARCHAR );')

    def CreatePackedDataTable(self):
        self.cursor.execute('CREATE TABLE packed_data ( idpack INTEGER PRIMARY KEY, data BLOB );')

    def dropAllIndexes(self):
        self.cursor.execute('SELECT name FROM SQLITE_MASTER WHERE type = "index"')
        indexes = self.fetchall2set(self.cursor.fetchall())
        for index in indexes:
            if not index.startswith("sqlite"):
                self.cursor.execute('DROP INDEX IF EXISTS %s' % (index,))

    def listAllIndexes(self, only_entropy = True):
        self.cursor.execute('SELECT name FROM SQLITE_MASTER WHERE type = "index"')
        indexes = self.fetchall2set(self.cursor.fetchall())
        if not only_entropy:
            return indexes
        myindexes = set()
        for index in indexes:
            if index.startswith("sqlite"):
                continue
            myindexes.add(index)
        return myindexes


    def createAllIndexes(self):
        self.createContentIndex()
        self.createBaseinfoIndex()
        self.createKeywordsIndex()
        self.createDependenciesIndex()
        self.createProvideIndex()
        self.createConflictsIndex()
        self.createExtrainfoIndex()
        self.createNeededIndex()
        self.createUseflagsIndex()
        self.createLicensedataIndex()
        self.createLicensesIndex()
        self.createConfigProtectReferenceIndex()
        self.createMessagesIndex()
        self.createSourcesIndex()
        self.createCountersIndex()
        self.createEclassesIndex()
        self.createCategoriesIndex()
        self.createCompileFlagsIndex()

    def createNeededIndex(self):
        if self.indexing:
            self.cursor.execute('CREATE INDEX IF NOT EXISTS neededindex ON neededreference ( library )')
            self.cursor.execute('CREATE INDEX IF NOT EXISTS neededindex_idneeded ON needed ( idneeded )')
            self.cursor.execute('CREATE INDEX IF NOT EXISTS neededindex_idpackage ON needed ( idpackage )')
            self.cursor.execute('CREATE INDEX IF NOT EXISTS neededindex_elfclass ON needed ( elfclass )')
            self.commitChanges()

    def createMessagesIndex(self):
        if self.indexing:
            self.cursor.execute('CREATE INDEX IF NOT EXISTS messagesindex ON messages ( idpackage )')
            self.commitChanges()

    def createCompileFlagsIndex(self):
        if self.indexing:
            self.cursor.execute('CREATE INDEX IF NOT EXISTS flagsindex ON flags ( chost,cflags,cxxflags )')
            self.commitChanges()

    def createUseflagsIndex(self):
        if self.indexing:
            self.cursor.execute('CREATE INDEX IF NOT EXISTS useflagsindex_useflags_idpackage ON useflags ( idpackage )')
            self.cursor.execute('CREATE INDEX IF NOT EXISTS useflagsindex_useflags_idflag ON useflags ( idflag )')
            self.cursor.execute('CREATE INDEX IF NOT EXISTS useflagsindex ON useflagsreference ( flagname )')
            self.commitChanges()

    def createContentIndex(self):
        if self.indexing:
            self.cursor.execute('CREATE INDEX IF NOT EXISTS contentindex_couple ON content ( idpackage )')
            self.cursor.execute('CREATE INDEX IF NOT EXISTS contentindex_file ON content ( file )')
            self.commitChanges()

    def createConfigProtectReferenceIndex(self):
        if self.indexing:
            self.cursor.execute('CREATE INDEX IF NOT EXISTS configprotectreferenceindex ON configprotectreference ( protect )')
            self.commitChanges()

    def createBaseinfoIndex(self):
        if self.indexing:
            self.cursor.execute('CREATE INDEX IF NOT EXISTS baseindex_atom ON baseinfo ( atom )')
            self.cursor.execute('CREATE INDEX IF NOT EXISTS baseindex_branch_name ON baseinfo ( name,branch )')
            self.cursor.execute('CREATE INDEX IF NOT EXISTS baseindex_branch_name_idcategory ON baseinfo ( name,idcategory,branch )')
            self.cursor.execute('CREATE INDEX IF NOT EXISTS baseindex_idcategory ON baseinfo ( idcategory )')
            self.commitChanges()

    def createLicensedataIndex(self):
        if self.indexing:
            if not self.doesTableExist("licensedata"):
                return
            self.cursor.execute('CREATE INDEX IF NOT EXISTS licensedataindex ON licensedata ( licensename )')
            self.commitChanges()

    def createLicensesIndex(self):
        if self.indexing:
            self.cursor.execute('CREATE INDEX IF NOT EXISTS licensesindex ON licenses ( license )')
            self.commitChanges()

    def createCategoriesIndex(self):
        if self.indexing:
            self.cursor.execute('CREATE INDEX IF NOT EXISTS categoriesindex_category ON categories ( category )')
            self.commitChanges()

    def createKeywordsIndex(self):
        if self.indexing:
            self.cursor.execute('CREATE INDEX IF NOT EXISTS keywordsreferenceindex ON keywordsreference ( keywordname )')
            self.cursor.execute('CREATE INDEX IF NOT EXISTS keywordsindex_idpackage ON keywords ( idpackage )')
            self.cursor.execute('CREATE INDEX IF NOT EXISTS keywordsindex_idkeyword ON keywords ( idkeyword )')
            self.commitChanges()

    def createDependenciesIndex(self):
        if self.indexing:
            self.cursor.execute('CREATE INDEX IF NOT EXISTS dependenciesindex_idpackage ON dependencies ( idpackage )')
            self.cursor.execute('CREATE INDEX IF NOT EXISTS dependenciesindex_iddependency ON dependencies ( iddependency )')
            self.cursor.execute('CREATE INDEX IF NOT EXISTS dependenciesreferenceindex_dependency ON dependenciesreference ( dependency )')
            self.commitChanges()

    def createCountersIndex(self):
        if self.indexing:
            self.cursor.execute('CREATE INDEX IF NOT EXISTS countersindex_counter ON counters ( counter )')
            self.commitChanges()

    def createSourcesIndex(self):
        if self.indexing:
            self.cursor.execute('CREATE INDEX IF NOT EXISTS sourcesindex_idpackage ON sources ( idpackage )')
            self.cursor.execute('CREATE INDEX IF NOT EXISTS sourcesindex_idsource ON sources ( idsource )')
            self.cursor.execute('CREATE INDEX IF NOT EXISTS sourcesreferenceindex_source ON sourcesreference ( source )')
            self.commitChanges()

    def createProvideIndex(self):
        if self.indexing:
            self.cursor.execute('CREATE INDEX IF NOT EXISTS provideindex_idpackage ON provide ( idpackage )')
            self.cursor.execute('CREATE INDEX IF NOT EXISTS provideindex_atom ON provide ( atom )')
            self.commitChanges()

    def createConflictsIndex(self):
        if self.indexing:
            self.cursor.execute('CREATE INDEX IF NOT EXISTS conflictsindex_idpackage ON conflicts ( idpackage )')
            self.cursor.execute('CREATE INDEX IF NOT EXISTS conflictsindex_atom ON conflicts ( conflict )')
            self.commitChanges()

    def createExtrainfoIndex(self):
        if self.indexing:
            self.cursor.execute('CREATE INDEX IF NOT EXISTS extrainfoindex ON extrainfo ( description )')
            self.commitChanges()

    def createEclassesIndex(self):
        if self.indexing:
            self.cursor.execute('CREATE INDEX IF NOT EXISTS eclassesindex_idpackage ON eclasses ( idpackage )')
            self.cursor.execute('CREATE INDEX IF NOT EXISTS eclassesindex_idclass ON eclasses ( idclass )')
            self.cursor.execute('CREATE INDEX IF NOT EXISTS eclassesreferenceindex_classname ON eclassesreference ( classname )')
            self.commitChanges()

    def regenerateCountersTable(self, vdb_path, output = False):
        self.checkReadOnly()
        self.createCountersTable()
        # assign a counter to an idpackage
        myids = self.listAllIdpackages()
        for myid in myids:
            # get atom
            myatom = self.retrieveAtom(myid)
            mybranch = self.retrieveBranch(myid)
            myatom = self.entropyTools.remove_tag(myatom)
            myatomcounterpath = vdb_path+myatom+"/"+etpConst['spm']['xpak_entries']['counter']
            if os.path.isfile(myatomcounterpath):
                try:
                    f = open(myatomcounterpath,"r")
                    counter = int(f.readline().strip())
                    f.close()
                except:
                    if output:
                        mytxt = "%s: %s: %s" % (
                            bold(_("ATTENTION")),
                            red(_("cannot open Spm counter file for")),
                            bold(myatom),
                        )
                        self.updateProgress(
                            mytxt,
                            importance = 1,
                            type = "warning"
                        )
                    continue
                # insert id+counter
                try:
                    self.cursor.execute(
                            'INSERT into counters VALUES '
                            '(?,?,?)', ( counter, myid, mybranch )
                    )
                except self.dbapi2.IntegrityError:
                    if output:
                        mytxt = "%s: %s: %s" % (
                            bold(_("ATTENTION")),
                            red(_("counter for atom is duplicated, ignoring")),
                            bold(myatom),
                        )
                        self.updateProgress(
                            mytxt,
                            importance = 1,
                            type = "warning"
                        )
                    continue
                    # don't trust counters, they might not be unique

        self.commitChanges()

    def clearTreeupdatesEntries(self, repository):
        self.checkReadOnly()
        if not self.doesTableExist("treeupdates"):
            self.createTreeupdatesTable()
        # treeupdates
        self.cursor.execute("DELETE FROM treeupdates WHERE repository = (?)", (repository,))
        self.commitChanges()

    def resetTreeupdatesDigests(self):
        self.checkReadOnly()
        self.cursor.execute('UPDATE treeupdates SET digest = "-1"')
        self.commitChanges()

    #
    # FIXME: remove these when 1.0 will be out
    #

    def migrateCountersTable(self):
        self.cursor.execute('DROP TABLE IF EXISTS counterstemp;')
        self.cursor.execute('CREATE TABLE counterstemp ( counter INTEGER, idpackage INTEGER PRIMARY KEY, branch VARCHAR );')
        self.cursor.execute('select * from counters')
        countersdata = self.cursor.fetchall()
        if countersdata:
            for row in countersdata:
                self.cursor.execute('INSERT INTO counterstemp VALUES = (?,?,?)',row)
        self.cursor.execute('DROP TABLE counters')
        self.cursor.execute('ALTER TABLE counterstemp RENAME TO counters')
        self.commitChanges()

    def createCategoriesdescriptionTable(self):
        self.cursor.execute('CREATE TABLE categoriesdescription ( category VARCHAR, locale VARCHAR, description VARCHAR );')

    def createTreeupdatesTable(self):
        self.cursor.execute('CREATE TABLE treeupdates ( repository VARCHAR PRIMARY KEY, digest VARCHAR );')

    def createTreeupdatesactionsTable(self):
        self.cursor.execute('CREATE TABLE treeupdatesactions ( idupdate INTEGER PRIMARY KEY AUTOINCREMENT, repository VARCHAR, command VARCHAR, branch VARCHAR, date VARCHAR );')

    def createSizesTable(self):
        self.cursor.execute('CREATE TABLE sizes ( idpackage INTEGER, size INTEGER );')

    def createEntropyMiscCountersTable(self):
        self.cursor.execute('CREATE TABLE entropy_misc_counters ( idtype INTEGER PRIMARY KEY, counter INTEGER );')

    def createDependenciesTypeColumn(self):
        self.cursor.execute('ALTER TABLE dependencies ADD COLUMN type INTEGER;')
        self.cursor.execute('UPDATE dependencies SET type = (?)', (0,))

    def createCountersBranchColumn(self):
        self.cursor.execute('ALTER TABLE counters ADD COLUMN branch VARCHAR;')
        idpackages = self.listAllIdpackages()
        for idpackage in idpackages:
            branch = self.retrieveBranch(idpackage)
            self.cursor.execute('UPDATE counters SET branch = (?) WHERE idpackage = (?)', (branch,idpackage,))

    def createTreeupdatesactionsDateColumn(self):
        try: # if database disk image is malformed, won't raise exception here
            self.cursor.execute('ALTER TABLE treeupdatesactions ADD COLUMN date VARCHAR;')
            mytime = str(self.entropyTools.getCurrentUnixTime())
            self.cursor.execute('UPDATE treeupdatesactions SET date = (?)', (mytime,))
        except:
            pass

    def createTreeupdatesactionsBranchColumn(self):
        try: # if database disk image is malformed, won't raise exception here
            self.cursor.execute('ALTER TABLE treeupdatesactions ADD COLUMN branch VARCHAR;')
            self.cursor.execute('UPDATE treeupdatesactions SET branch = (?)', (str(etpConst['branch']),))
        except:
            pass

    def createNeededElfclassColumn(self):
        try: # if database disk image is malformed, won't raise exception here
            self.cursor.execute('ALTER TABLE needed ADD COLUMN elfclass INTEGER;')
            self.cursor.execute('UPDATE needed SET elfclass = -1')
        except:
            pass

    def createContentTypeColumn(self):
        try: # if database disk image is malformed, won't raise exception here
            self.cursor.execute('ALTER TABLE content ADD COLUMN type VARCHAR;')
            self.cursor.execute('UPDATE content SET type = "0"')
        except:
            pass

    def createLicensedataTable(self):
        self.cursor.execute('CREATE TABLE licensedata ( licensename VARCHAR UNIQUE, text BLOB, compressed INTEGER );')

    def createLicensesAcceptedTable(self):
        self.cursor.execute('CREATE TABLE licenses_accepted ( licensename VARCHAR UNIQUE );')

    def createTrashedcountersTable(self):
        self.cursor.execute('CREATE TABLE trashedcounters ( counter INTEGER );')

    def createTriggerTable(self):
        self.cursor.execute('CREATE TABLE triggers ( idpackage INTEGER PRIMARY KEY, data BLOB );')

    def createTriggerColumn(self):
        self.checkReadOnly()
        self.cursor.execute('ALTER TABLE baseinfo ADD COLUMN trigger INTEGER;')
        self.cursor.execute('UPDATE baseinfo SET trigger = 0')

    def createMessagesTable(self):
        self.cursor.execute("CREATE TABLE messages ( idpackage INTEGER, message VARCHAR );")

    def createEclassesTable(self):
        self.cursor.execute('DROP TABLE IF EXISTS eclasses;')
        self.cursor.execute('DROP TABLE IF EXISTS eclassesreference;')
        self.cursor.execute('CREATE TABLE eclasses ( idpackage INTEGER, idclass INTEGER );')
        self.cursor.execute('CREATE TABLE eclassesreference ( idclass INTEGER PRIMARY KEY AUTOINCREMENT, classname VARCHAR );')

    def createNeededTable(self):
        self.cursor.execute('CREATE TABLE needed ( idpackage INTEGER, idneeded INTEGER, elfclass INTEGER );')
        self.cursor.execute('CREATE TABLE neededreference ( idneeded INTEGER PRIMARY KEY AUTOINCREMENT, library VARCHAR );')

    def createSystemPackagesTable(self):
        self.cursor.execute('CREATE TABLE systempackages ( idpackage INTEGER PRIMARY KEY );')

    def createInjectedTable(self):
        self.cursor.execute('CREATE TABLE injected ( idpackage INTEGER PRIMARY KEY );')

    def createProtectTable(self):
        self.cursor.execute('DROP TABLE IF EXISTS configprotect;')
        self.cursor.execute('DROP TABLE IF EXISTS configprotectmask;')
        self.cursor.execute('DROP TABLE IF EXISTS configprotectreference;')
        self.cursor.execute('CREATE TABLE configprotect ( idpackage INTEGER PRIMARY KEY, idprotect INTEGER );')
        self.cursor.execute('CREATE TABLE configprotectmask ( idpackage INTEGER PRIMARY KEY, idprotect INTEGER );')
        self.cursor.execute('CREATE TABLE configprotectreference ( idprotect INTEGER PRIMARY KEY AUTOINCREMENT, protect VARCHAR );')

    def createInstalledTable(self):
        self.cursor.execute('DROP TABLE IF EXISTS installedtable;')
        self.cursor.execute('CREATE TABLE installedtable ( idpackage INTEGER PRIMARY KEY, repositoryname VARCHAR );')

    def addDependRelationToDependsTable(self, iddependency, idpackage):
        self.cursor.execute(
                'INSERT into dependstable VALUES '
                '(?,?)'
                , (	iddependency,
                        idpackage,
                        )
        )
        if (self.entropyTools.is_user_in_entropy_group()) and \
            (self.dbname.startswith(etpConst['serverdbid'])):
                # force commit even if readonly, this will allow to automagically fix dependstable server side
                self.connection.commit() # we don't care much about syncing the database since it's quite trivial

    '''
       @description: recreate dependstable table in the chosen database, it's used for caching searchDepends requests
       @input Nothing
       @output: Nothing
    '''
    def regenerateDependsTable(self, output = True):
        self.createDependsTable()
        depends = self.listAllDependencies()
        count = 0
        total = len(depends)
        for iddep,atom in depends:
            count += 1
            if output:
                self.updateProgress(
                                        red("Resolving %s") % (atom,),
                                        importance = 0,
                                        type = "info",
                                        back = True,
                                        count = (count,total)
                                    )
            idpackage, rc = self.atomMatch(atom)
            if (idpackage != -1):
                self.addDependRelationToDependsTable(iddep,idpackage)
        del depends
        # now validate dependstable
        self.sanitizeDependsTable()


########################################################
####
##   Dependency handling functions
#

    def atomMatchFetchCache(self, *args):
        if self.xcache:
            c_hash = str(hash(tuple(args)))
            try:
                cached = self.dumpTools.loadobj(etpCache['dbMatch']+"/"+self.dbname+"/"+c_hash)
                if cached != None:
                    return cached
            except (EOFError, IOError):
                return None

    def atomMatchStoreCache(self, *args, **kwargs):
        if self.xcache:
            c_hash = str(hash(tuple(args)))
            try:
                sperms = False
                if not os.path.isdir(os.path.join(etpConst['dumpstoragedir'],etpCache['dbMatch']+"/"+self.dbname)):
                    sperms = True
                self.dumpTools.dumpobj(etpCache['dbMatch']+"/"+self.dbname+"/"+c_hash,kwargs['result'])
                if sperms:
                    const_setup_perms(etpConst['dumpstoragedir'],etpConst['entropygid'])
            except IOError:
                pass

    # function that validate one atom by reading keywords settings
    # idpackageValidatorCache = {} >> function cache
    def idpackageValidator(self,idpackage):

        if self.dbname == etpConst['clientdbid']:
            return idpackage,0

        reponame = self.dbname[5:]
        cached = idpackageValidatorCache.get((idpackage,reponame))
        if cached != None:
            return cached

        # check if user package.mask needs it masked
        user_package_mask_ids = etpConst['packagemasking'].get(reponame+'mask_ids')
        if user_package_mask_ids == None:
            etpConst['packagemasking'][reponame+'mask_ids'] = set()
            for atom in etpConst['packagemasking']['mask']:
                matches = self.atomMatch(atom, multiMatch = True, packagesFilter = False)
                if matches[1] != 0:
                    continue
                etpConst['packagemasking'][reponame+'mask_ids'] |= set(matches[0])
            user_package_mask_ids = etpConst['packagemasking'][reponame+'mask_ids']
        if idpackage in user_package_mask_ids:
            # sorry, masked
            idpackageValidatorCache[(idpackage,reponame)] = -1,1
            return -1,1

        # see if we can unmask by just lookin into user package.unmask stuff -> etpConst['packagemasking']['unmask']
        user_package_unmask_ids = etpConst['packagemasking'].get(reponame+'unmask_ids')
        if user_package_unmask_ids == None:
            etpConst['packagemasking'][reponame+'unmask_ids'] = set()
            for atom in etpConst['packagemasking']['unmask']:
                matches = self.atomMatch(atom, multiMatch = True, packagesFilter = False)
                if matches[1] != 0:
                    continue
                etpConst['packagemasking'][reponame+'unmask_ids'] |= set(matches[0])
            user_package_unmask_ids = etpConst['packagemasking'][reponame+'unmask_ids']
        if idpackage in user_package_unmask_ids:
            idpackageValidatorCache[(idpackage,reponame)] = idpackage,3
            return idpackage,3

        # check if repository packages.db.mask needs it masked
        repomask = etpConst['packagemasking']['repos_mask'].get(reponame)
        if repomask != None:
            # first, seek into generic masking, all branches
            all_branches_mask = repomask.get("*")
            if all_branches_mask:
                all_branches_mask_ids = repomask.get("*_ids")
                if all_branches_mask_ids == None:
                    etpConst['packagemasking']['repos_mask'][reponame]['*_ids'] = set()
                    for atom in all_branches_mask:
                        matches = self.atomMatch(atom, multiMatch = True, packagesFilter = False)
                        if matches[1] != 0:
                            continue
                        etpConst['packagemasking']['repos_mask'][reponame]['*_ids'] |= set(matches[0])
                    all_branches_mask_ids = etpConst['packagemasking']['repos_mask'][reponame]['*_ids']
                if idpackage in all_branches_mask_ids:
                    idpackageValidatorCache[(idpackage,reponame)] = -1,8
                    return -1,8
            # no universal mask
            branches_mask = repomask.get("branch")
            if branches_mask:
                for branch in branches_mask:
                    branch_mask_ids = branches_mask.get(branch+"_ids")
                    if branch_mask_ids == None:
                        etpConst['packagemasking']['repos_mask'][reponame]['branch'][branch+"_ids"] = set()
                        for atom in branches_mask[branch]:
                            matches = self.atomMatch(atom, multiMatch = True, packagesFilter = False)
                            if matches[1] != 0:
                                continue
                            etpConst['packagemasking']['repos_mask'][reponame]['branch'][branch+"_ids"] |= set(matches[0])
                        branch_mask_ids = etpConst['packagemasking']['repos_mask'][reponame]['branch'][branch+"_ids"]
                    if idpackage in branch_mask_ids:
                        if  self.retrieveBranch(idpackage) == branch:
                            idpackageValidatorCache[(idpackage,reponame)] = -1,9
                            return -1,9

        if etpConst['packagemasking']['license_mask']:
            mylicenses = self.retrieveLicense(idpackage)
            mylicenses = mylicenses.strip().split()
            if mylicenses:
                for mylicense in mylicenses:
                    if mylicense in etpConst['packagemasking']['license_mask']:
                        idpackageValidatorCache[(idpackage,reponame)] = -1,10
                        return -1,10

        mykeywords = self.retrieveKeywords(idpackage)
        # XXX WORKAROUND
        if not mykeywords: mykeywords = [''] # ** is fine then
        # firstly, check if package keywords are in etpConst['keywords']
        # (universal keywords have been merged from package.mask)
        for key in etpConst['keywords']:
            if key in mykeywords:
                # found! all fine
                idpackageValidatorCache[(idpackage,reponame)] = idpackage,2
                return idpackage,2

        # if we get here, it means we didn't find mykeywords in etpConst['keywords']
        # we need to seek etpConst['packagemasking']['keywords']
        # seek in repository first
        if reponame in etpConst['packagemasking']['keywords']['repositories']:
            for keyword in etpConst['packagemasking']['keywords']['repositories'][reponame]:
                if keyword in mykeywords:
                    keyword_data = etpConst['packagemasking']['keywords']['repositories'][reponame].get(keyword)
                    if keyword_data:
                        if "*" in keyword_data: # all packages in this repo with keyword "keyword" are ok
                            idpackageValidatorCache[(idpackage,reponame)] = idpackage,4
                            return idpackage,4
                        keyword_data_ids = etpConst['packagemasking']['keywords']['repositories'][reponame].get(keyword+"_ids")
                        if keyword_data_ids == None:
                            etpConst['packagemasking']['keywords']['repositories'][reponame][keyword+"_ids"] = set()
                            for atom in keyword_data:
                                matches = self.atomMatch(atom, multiMatch = True, packagesFilter = False)
                                if matches[1] != 0:
                                    continue
                                etpConst['packagemasking']['keywords']['repositories'][reponame][keyword+"_ids"] |= matches[0]
                            keyword_data_ids = etpConst['packagemasking']['keywords']['repositories'][reponame][keyword+"_ids"]
                        if idpackage in keyword_data_ids:
                            idpackageValidatorCache[(idpackage,reponame)] = idpackage,5
                            return idpackage,5

        # if we get here, it means we didn't find a match in repositories
        # so we scan packages, last chance
        for keyword in etpConst['packagemasking']['keywords']['packages']:
            # first of all check if keyword is in mykeywords
            if keyword in mykeywords:
                keyword_data = etpConst['packagemasking']['keywords']['packages'].get(keyword)
                # check for relation
                if keyword_data:
                    keyword_data_ids = etpConst['packagemasking']['keywords']['packages'][reponame+keyword+"_ids"]
                    if keyword_data_ids == None:
                        etpConst['packagemasking']['keywords']['packages'][reponame+keyword+"_ids"] = set()
                        for atom in keyword_data:
                            # match atom
                            matches = self.atomMatch(atom, multiMatch = True, packagesFilter = False)
                            if matches[1] != 0:
                                continue
                            etpConst['packagemasking']['keywords']['packages'][reponame+keyword+"_ids"] |= matches[0]
                        keyword_data_ids = etpConst['packagemasking']['keywords']['packages'][reponame+keyword+"_ids"]
                    if idpackage in keyword_data_ids:
                        # valid!
                        idpackageValidatorCache[(idpackage,reponame)] = idpackage,6
                        return idpackage,6

        # holy crap, can't validate
        idpackageValidatorCache[(idpackage,reponame)] = -1,7
        return -1,7

    # packages filter used by atomMatch, input must me foundIDs, a list like this:
    # [608,1867]
    def packagesFilter(self, results, atom):
        # keywordsFilter ONLY FILTERS results if
        # self.dbname.startswith(etpConst['dbnamerepoprefix']) => repository database is open
        if not self.dbname.startswith(etpConst['dbnamerepoprefix']):
            return results

        newresults = set()
        for idpackage in results:
            rc = self.idpackageValidator(idpackage)
            if rc[0] != -1:
                newresults.add(idpackage)
            else:
                idreason = rc[1]
                if not maskingReasonsStorage.has_key(atom):
                    maskingReasonsStorage[atom] = {}
                if not maskingReasonsStorage[atom].has_key(idreason):
                    maskingReasonsStorage[atom][idreason] = set()
                maskingReasonsStorage[atom][idreason].add((idpackage,self.dbname[5:]))
        return newresults

    def __filterSlot(self, idpackage, slot):
        if slot == None:
            return idpackage
        dbslot = self.retrieveSlot(idpackage)
        if str(dbslot) == str(slot):
            return idpackage

    def __filterTag(self, idpackage, tag, operators):
        if tag == None:
            return idpackage
        dbtag = self.retrieveVersionTag(idpackage)
        compare = cmp(tag,dbtag)
        if not operators or operators == "=":
            if compare == 0:
                return idpackage
        else:
            return self.__do_operator_compare(idpackage, operators, compare)

    def __do_operator_compare(self, token, operators, compare):
        if operators == ">" and compare == -1:
            return token
        elif operators == ">=" and compare < 1:
            return token
        elif operators == "<" and compare == 1:
            return token
        elif operators == "<=" and compare > -1:
            return token

    def __filterSlotTag(self, foundIDs, slot, tag, operators):

        newlist = set()
        for idpackage in foundIDs:

            idpackage = self.__filterSlot(idpackage, slot)
            if not idpackage:
                continue

            idpackage = self.__filterTag(idpackage, tag, operators)
            if not idpackage:
                continue

            newlist.add(idpackage)

        return newlist

    '''
       @description: matches the user chosen package name+ver, if possibile, in a single repository
       @input atom: string, atom to match
       @input caseSensitive: bool, should the atom be parsed case sensitive?
       @input matchSlot: string, match atoms with the provided slot
       @input multiMatch: bool, return all the available atoms
       @input matchBranches: tuple or list, match packages only in the specified branches
       @input matchTag: match packages only for the specified tag
       @input packagesFilter: enable/disable package.mask/.keywords/.unmask filter
       @output: the package id, if found, otherwise -1 plus the status, 0 = ok, 1 = error
    '''
    def atomMatch(self, atom, caseSensitive = True, matchSlot = None, multiMatch = False, matchBranches = (), matchTag = None, packagesFilter = True, matchRevision = None, extendedResults = False):

        if not atom:
            return -1,1

        cached = self.atomMatchFetchCache(
            atom,
            caseSensitive,
            matchSlot,
            multiMatch,
            matchBranches,
            matchTag,
            packagesFilter,
            matchRevision,
            extendedResults
        )
        if cached != None:
            return cached

        atomTag = self.entropyTools.dep_gettag(atom)
        atomSlot = self.entropyTools.dep_getslot(atom)
        atomRev = self.entropyTools.dep_get_entropy_revision(atom)

        # tag match
        scan_atom = self.entropyTools.remove_tag(atom)
        if (matchTag == None) and (atomTag != None):
            matchTag = atomTag

        # slot match
        scan_atom = self.entropyTools.remove_slot(scan_atom)
        if (matchSlot == None) and (atomSlot != None):
            matchSlot = atomSlot

        # revision match
        scan_atom = self.entropyTools.remove_entropy_revision(scan_atom)
        if (matchRevision == None) and (atomRev != None):
            matchRevision = atomRev

        # check for direction
        strippedAtom = self.entropyTools.dep_getcpv(scan_atom)
        if scan_atom[-1] == "*":
            strippedAtom += "*"
        direction = scan_atom[0:len(scan_atom)-len(strippedAtom)]

        justname = self.entropyTools.isjustname(strippedAtom)
        pkgversion = ''
        if not justname:

            # get version
            data = self.entropyTools.catpkgsplit(strippedAtom)
            if data == None:
                return -1,1 # atom is badly formatted
            pkgversion = data[2]+"-"+data[3]

        pkgkey = self.entropyTools.dep_getkey(strippedAtom)
        splitkey = pkgkey.split("/")
        if (len(splitkey) == 2):
            pkgname = splitkey[1]
            pkgcat = splitkey[0]
        else:
            pkgname = splitkey[0]
            pkgcat = "null"

        if matchBranches:
            # force to tuple for security
            myBranchIndex = tuple(matchBranches)
        else:
            if self.dbname == etpConst['clientdbid']:
                # collect all available branches
                myBranchIndex = tuple(self.listAllBranches())
            elif self.dbname.startswith(etpConst['dbnamerepoprefix']):
                # repositories should match to any branch <= than the current if none specified
                allbranches = set([x for x in self.listAllBranches() if x <= etpConst['branch']])
                allbranches = list(allbranches)
                allbranches.reverse()
                if etpConst['branch'] not in allbranches:
                    allbranches.insert(0,etpConst['branch'])
                myBranchIndex = tuple(allbranches)
            else:
                myBranchIndex = (etpConst['branch'],)

        # IDs found in the database that match our search
        foundIDs = set()

        for idx in myBranchIndex:

            if pkgcat == "null":
                results = self.searchPackagesByName(
                                    pkgname,
                                    sensitive = caseSensitive,
                                    branch = idx,
                                    justid = True
                )
            else:
                results = self.searchPackagesByNameAndCategory(
                                    name = pkgname,
                                    category = pkgcat,
                                    branch = idx,
                                    sensitive = caseSensitive,
                                    justid = True
                )

            mypkgcat = pkgcat
            mypkgname = pkgname
            virtual = False
            # if it's a PROVIDE, search with searchProvide
            # there's no package with that name
            if (not results) and (mypkgcat == "virtual"):
                virtuals = self.searchProvide(pkgkey, branch = idx, justid = True)
                if virtuals:
                    virtual = True
                    mypkgname = self.retrieveName(virtuals[0])
                    mypkgcat = self.retrieveCategory(virtuals[0])
                    results = virtuals

            # now validate
            if not results:
                continue # search into a stabler branch

            elif (len(results) > 1):

                # if it's because category differs, it's a problem
                foundCat = None
                cats = set()
                for idpackage in results:
                    cat = self.retrieveCategory(idpackage)
                    cats.add(cat)
                    if (cat == mypkgcat) or ((not virtual) and (mypkgcat == "virtual") and (cat == mypkgcat)):
                        # in case of virtual packages only (that they're not stored as provide)
                        foundCat = cat

                # if we found something at least...
                if (not foundCat) and (len(cats) == 1) and (mypkgcat in ("virtual","null")):
                    foundCat = list(cats)[0]

                if not foundCat:
                    # got the issue
                    continue

                # we can use foundCat
                mypkgcat = foundCat

                # we need to search using the category
                if (not multiMatch) and (pkgcat == "null" or virtual):
                    # we searched by name, we need to search using category
                    results = self.searchPackagesByNameAndCategory(
                                        name = mypkgname,
                                        category = mypkgcat,
                                        branch = idx,
                                        sensitive = caseSensitive,
                                        justid = True
                    )

                # validate again
                if not results:
                    continue  # search into another branch

                # if we get here, we have found the needed IDs
                foundIDs |= set(results)
                break

            else:

                idpackage = results[0]
                # if mypkgcat is virtual, we can force
                if (mypkgcat == "virtual") and (not virtual):
                    # in case of virtual packages only (that they're not stored as provide)
                    mypkgcat = self.retrieveCategory(idpackage)

                # check if category matches
                if mypkgcat != "null":
                    foundCat = self.retrieveCategory(idpackage)
                    if mypkgcat == foundCat:
                        foundIDs.add(idpackage)
                    else:
                        continue
                else:
                    foundIDs.add(idpackage)
                    break

        ### FILTERING
        ### FILTERING
        ### FILTERING

        # filter slot and tag
        foundIDs = self.__filterSlotTag(foundIDs, matchSlot, matchTag, direction)

        if packagesFilter: # keyword filtering
            foundIDs = self.packagesFilter(foundIDs, atom)

        ### END FILTERING
        ### END FILTERING
        ### END FILTERING

        if not foundIDs:
            # package not found
            self.atomMatchStoreCache(atom, caseSensitive, matchSlot, multiMatch, matchBranches, matchTag, packagesFilter, matchRevision, extendedResults, result = (-1,1))
            return -1,1

        ### FILLING dbpkginfo
        ### FILLING dbpkginfo
        ### FILLING dbpkginfo

        dbpkginfo = set()
        # now we have to handle direction
        if (direction) or (direction == '' and not justname) or (direction == '' and not justname and strippedAtom.endswith("*")):

            if (not justname) and \
                ((direction == "~") or (direction == "=") or \
                (direction == '' and not justname) or (direction == '' and not justname and strippedAtom.endswith("*"))):
                # any revision within the version specified OR the specified version

                if (direction == '' and not justname):
                    direction = "="

                # remove gentoo revision (-r0 if none)
                if (direction == "="):
                    if (pkgversion.split("-")[-1] == "r0"):
                        pkgversion = self.entropyTools.remove_revision(pkgversion)
                if (direction == "~"):
                    pkgrevision = self.entropyTools.dep_get_portage_revision(pkgversion)
                    pkgversion = self.entropyTools.remove_revision(pkgversion)

                for idpackage in foundIDs:

                    dbver = self.retrieveVersion(idpackage)
                    if (direction == "~"):
                        myrev = self.entropyTools.dep_get_portage_revision(dbver)
                        myver = self.entropyTools.remove_revision(dbver)
                        if myver == pkgversion and pkgrevision <= myrev:
                            # found
                            dbpkginfo.add((idpackage,dbver))
                    else:
                        # media-libs/test-1.2* support
                        if pkgversion[-1] == "*":
                            if dbver.startswith(pkgversion[:-1]):
                                dbpkginfo.add((idpackage,dbver))
                        elif (matchRevision != None) and (pkgversion == dbver):
                            dbrev = self.retrieveRevision(idpackage)
                            if dbrev == matchRevision:
                                dbpkginfo.add((idpackage,dbver))
                        elif (pkgversion == dbver) and (matchRevision == None):
                            dbpkginfo.add((idpackage,dbver))

            elif (direction.find(">") != -1) or (direction.find("<") != -1):

                if not justname:

                    # remove revision (-r0 if none)
                    if pkgversion.endswith("r0"):
                        # remove
                        self.entropyTools.remove_revision(pkgversion)

                    for idpackage in foundIDs:

                        revcmp = 0
                        tagcmp = 0
                        if matchRevision != None:
                            dbrev = self.retrieveRevision(idpackage)
                            revcmp = cmp(matchRevision,dbrev)
                        if matchTag != None:
                            dbtag = self.retrieveVersionTag(idpackage)
                            tagcmp = cmp(matchTag,dbtag)
                        dbver = self.retrieveVersion(idpackage)
                        pkgcmp = self.entropyTools.compareVersions(pkgversion,dbver)
                        if isinstance(pkgcmp,tuple):
                            failed = pkgcmp[1]
                            if failed == 0:
                                failed = pkgversion
                            else:
                                failed = dbver
                            # I am sorry, but either pkgversion or dbver are invalid
                            self.updateProgress(
                                bold("atomMatch: ")+red("%s %s %s %s %s. %s: %s") % (
                                    _("comparison between"),
                                    pkgversion,
                                    _("and"),
                                    dbver,
                                    _("failed"),
                                    _("Wrong syntax for"),
                                    failed,
                                ),
                                importance = 1,
                                type = "error",
                                header = darkred(" !!! ")
                            )
                            mytxt = "%s: %s, cmp(): %s, %s: %s" % (
                                _("from atom"),
                                atom,
                                pkgcmp,
                                _("failed"),
                                failed,
                            )
                            raise exceptionTools.InvalidVersionString("InvalidVersionString: %s" % (mytxt, ))
                        if direction == ">":
                            if pkgcmp < 0:
                                dbpkginfo.add((idpackage,dbver))
                            elif (matchRevision != None) and pkgcmp <= 0 and revcmp < 0:
                                #print "found >",self.retrieveAtom(idpackage)
                                dbpkginfo.add((idpackage,dbver))
                            elif (matchTag != None) and tagcmp < 0:
                                dbpkginfo.add((idpackage,dbver))
                        elif direction == "<":
                            if pkgcmp > 0:
                                dbpkginfo.add((idpackage,dbver))
                            elif (matchRevision != None) and pkgcmp >= 0 and revcmp > 0:
                                #print "found <",self.retrieveAtom(idpackage)
                                dbpkginfo.add((idpackage,dbver))
                            elif (matchTag != None) and tagcmp > 0:
                                dbpkginfo.add((idpackage,dbver))
                        elif direction == ">=":
                            if (matchRevision != None) and pkgcmp <= 0:
                                if pkgcmp == 0:
                                    if revcmp <= 0:
                                        dbpkginfo.add((idpackage,dbver))
                                        #print "found >=",self.retrieveAtom(idpackage)
                                else:
                                    dbpkginfo.add((idpackage,dbver))
                            elif pkgcmp <= 0 and matchRevision == None:
                                dbpkginfo.add((idpackage,dbver))
                            elif (matchTag != None) and tagcmp <= 0:
                                dbpkginfo.add((idpackage,dbver))
                        elif direction == "<=":
                            if (matchRevision != None) and pkgcmp >= 0:
                                if pkgcmp == 0:
                                    if revcmp >= 0:
                                        dbpkginfo.add((idpackage,dbver))
                                        #print "found <=",self.retrieveAtom(idpackage)
                                else:
                                    dbpkginfo.add((idpackage,dbver))
                            elif pkgcmp >= 0 and matchRevision == None:
                                dbpkginfo.add((idpackage,dbver))
                            elif (matchTag != None) and tagcmp >= 0:
                                dbpkginfo.add((idpackage,dbver))

        else: # just the key

            dbpkginfo = set([(x,self.retrieveVersion(x)) for x in foundIDs])

        ### END FILLING dbpkginfo
        ### END FILLING dbpkginfo
        ### END FILLING dbpkginfo

        if not dbpkginfo:
            if extendedResults:
                x = (-1,1,None,None,None)
                self.atomMatchStoreCache(
                    atom,
                    caseSensitive,
                    matchSlot,
                    multiMatch,
                    matchBranches,
                    matchTag,
                    packagesFilter,
                    matchRevision,
                    extendedResults,
                    result = x
                )
                return x
            else:
                self.atomMatchStoreCache(
                    atom,
                    caseSensitive,
                    matchSlot,
                    multiMatch,
                    matchBranches,
                    matchTag,
                    packagesFilter,
                    matchRevision,
                    extendedResults,
                    result = (-1,1)
                )
                return -1,1

        if multiMatch:
            if extendedResults:
                x = set([(x[0],0,x[1],self.retrieveVersionTag(x[0]),self.retrieveRevision(x[0])) for x in dbpkginfo]),0
                self.atomMatchStoreCache(
                    atom,
                    caseSensitive,
                    matchSlot,
                    multiMatch,
                    matchBranches,
                    matchTag,
                    packagesFilter,
                    matchRevision,
                    extendedResults,
                    result = x
                )
                return x
            else:
                x = set([x[0] for x in dbpkginfo])
                self.atomMatchStoreCache(
                    atom,
                    caseSensitive,
                    matchSlot,
                    multiMatch,
                    matchBranches,
                    matchTag,
                    packagesFilter,
                    matchRevision,
                    extendedResults,
                    result = (x,0)
                )
                return x,0

        if len(dbpkginfo) == 1:
            x = dbpkginfo.pop()
            if extendedResults:
                x = (x[0],0,x[1],self.retrieveVersionTag(x[0]),self.retrieveRevision(x[0])),0
                self.atomMatchStoreCache(
                    atom,
                    caseSensitive,
                    matchSlot,
                    multiMatch,
                    matchBranches,
                    matchTag,
                    packagesFilter,
                    matchRevision,
                    extendedResults,
                    result = x
                )
                return x
            else:
                self.atomMatchStoreCache(
                    atom,
                    caseSensitive,
                    matchSlot,
                    multiMatch,
                    matchBranches,
                    matchTag,
                    packagesFilter,
                    matchRevision,
                    extendedResults,
                    result = (x[0],0)
                )
                return x[0],0

        dbpkginfo = list(dbpkginfo)
        pkgdata = {}
        versions = set()
        for x in dbpkginfo:
            info_tuple = (x[1],self.retrieveVersionTag(x[0]),self.retrieveRevision(x[0]))
            versions.add(info_tuple)
            pkgdata[info_tuple] = x[0]
        newer = self.entropyTools.getEntropyNewerVersion(list(versions))[0]
        x = pkgdata[newer]
        if extendedResults:
            x = (x,0,newer[0],newer[1],newer[2]),0
            self.atomMatchStoreCache(
                atom,
                caseSensitive,
                matchSlot,
                multiMatch,
                matchBranches,
                matchTag,
                packagesFilter,
                matchRevision,
                extendedResults,
                result = x
            )
            return x
        else:
            self.atomMatchStoreCache(
                atom,
                caseSensitive,
                matchSlot,
                multiMatch,
                matchBranches,
                matchTag,
                packagesFilter,
                matchRevision,
                extendedResults,
                result = (x,0)
            )
            return x,0
