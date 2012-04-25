# -*- coding: utf-8 -*-
"""

    @author: Fabio Erculiani <lxnay@sabayon.org>
    @contact: lxnay@sabayon.org
    @copyright: Fabio Erculiani
    @license: GPL-2

    B{Entropy Package Manager Client}.

"""

########################################################
####
##   Repositories Tools
#
import os
import sys
import time
import copy

from entropy.const import etpConst, etpUi, const_debug_write
from entropy.output import red, darkred, blue, brown, bold, darkgreen, green, \
    print_info, print_warning, print_error, purple, teal
from entropy.core.settings.base import SystemSettings as SysSet
from entropy.misc import ParallelTask
from entropy.i18n import _
from entropy.services.client import WebService

from entropy.cli import print_table, get_entropy_webservice

import entropy.tools
SystemSettings = SysSet()

def repositories(options):

    # Options available for all the packages submodules
    myopts = options[1:]
    cmd = options[0]
    e_req_force_update = False
    e_req_conflicts = False
    rc = 0
    repo_names = []

    for opt in myopts:
        if opt == "--force":
            e_req_force_update = True
        elif (opt == "--conflicts") and (cmd == "repo"):
            e_req_conflicts = True
        elif opt.startswith("--"):
            print_error(red(" %s." % (_("Wrong parameters"),) ))
            return -10
        elif opt in SystemSettings['repositories']['order']:
            repo_names.append(opt)

    from entropy.client.interfaces import Client
    entropy_client = None
    acquired = False
    try:
        entropy_client = Client(installed_repo = False)
        acquired = entropy.tools.acquire_entropy_locks(entropy_client)
        if not acquired:
            print_error(darkgreen(_("Another Entropy is currently running.")))
            return 1

        if cmd == "update":
            # check if I am root
            er_txt = darkred(_("You must be either root or in this group:")) + \
                " " +  etpConst['sysgroup']
            if not entropy.tools.is_user_in_entropy_group():
                print_error(er_txt)
                return 1
            if not entropy.tools.is_root():
                rc = _do_dbus_sync(repo_names, force=e_req_force_update)
            else:
                rc = _do_sync(entropy_client, repo_identifiers = repo_names,
                    force = e_req_force_update)

        elif cmd == "status":
            for repo in SystemSettings['repositories']['order']:
                _show_repository_info(entropy_client, repo)

        elif cmd == "repo":

            er_txt = darkred(_("You must be root"))
            if not entropy.tools.is_root():
                print_error(er_txt)
                return 1

            myopts = options[1:]
            if not myopts:
                rc = -10
            else:
                repo_opt = myopts.pop(0)
                if not myopts:
                    rc = -10
                elif repo_opt == "enable":
                    rc = _enable_repositories(entropy_client, myopts)
                elif repo_opt == "disable":
                    rc = _disable_repositories(entropy_client, myopts)
                elif repo_opt == "add":
                    rc = _add_repository(entropy_client, myopts)
                elif repo_opt == "remove":
                    rc = _remove_repository(entropy_client, myopts)
                elif repo_opt == "mirrorsort":
                    rc = _mirror_sort(entropy_client, myopts)
                elif repo_opt == "merge":
                    myopts = [x for x in myopts if x not in ("--conflicts",)]
                    rc = _merge_repository(entropy_client, myopts,
                        remove_conflicts = e_req_conflicts)
                else:
                    rc = -10

        elif cmd == "notice":
            myopts = options[1:]
            myopts = [x for x in myopts if x in \
                SystemSettings['repositories']['available']]
            if not myopts:
                rc = -10
            else:
                rc = 0
                for repoid in myopts:
                    _notice_board_reader(entropy_client, repoid)
        else:
            rc = -10
    finally:
        if acquired and (entropy_client is not None):
            entropy.tools.release_entropy_locks(entropy_client)
        if entropy_client is not None:
            entropy_client.shutdown()

    return rc

def _add_repository(entropy_client, repo_strings):

    current_branch = SystemSettings['repositories']['branch']
    current_product = SystemSettings['repositories']['product']
    available_repos = SystemSettings['repositories']['available']

    for repo_string in repo_strings:
        if not repo_string:
            print_warning("[%s] %s" % (
                purple(repo_string), blue(_("invalid data, skipping")),))
            continue

        print_info("%s: %s" % (
            teal(_("Adding repository string")), blue(repo_string),))

        try:
            repoid, repodata = SystemSettings._analyze_client_repo_string(
                repo_string, current_branch, current_product)
        except AttributeError as err:
            print_warning("[%s] %s: %s" % (
                purple(repo_string),
                blue(_("invalid repository string")),
                err,)
            )
            continue

        # print some info
        toc = []
        toc.append((purple(_("Repository id:")), teal(repoid)))
        toc.append((darkgreen(_("Description:")), teal(repodata['description'])))
        toc.append((purple(_("Repository format:")), darkgreen(repodata['dbcformat'])))
        for pkg_url in repodata['plain_packages']:
            toc.append((purple(_("Packages URL:")), pkg_url))
        db_url = repodata['plain_database']
        if not db_url:
            db_url = _("None given")
        toc.append((purple(_("Repository URL:")), darkgreen(db_url)))
        toc.append(" ")
        print_table(toc)
        added = entropy_client.add_repository(repodata)
        if added:
            print_info("[%s] %s" % (
                    purple(repoid), blue(_("repository added succesfully")),))
        else:
            print_warning("[%s] %s" % (
                    purple(repoid), blue(_("cannot add repository")),))

    return 0

def _remove_repository(entropy_client, repo_ids):

    excluded_repos = SystemSettings['repositories']['excluded']
    available_repos = SystemSettings['repositories']['available']
    repos = set(list(excluded_repos.keys()) + list(available_repos.keys()))
    for repo_id in repo_ids:
        if repo_id not in repos:
            print_warning("[%s] %s" % (
                purple(repo_id), blue(_("repository id not available")),))
            continue

        removed = entropy_client.remove_repository(repo_id)
        if removed:
            print_info("[%s] %s" % (
                    purple(repo_id),
                    blue(_("repository removed succesfully")),))
        else:
            print_warning("[%s] %s" % (
                    purple(repo_id),
                    blue(_("cannot remove repository")),))

    return 0

def _enable_repositories(entropy_client, repos):
    excluded_repos = SystemSettings['repositories']['excluded']
    available_repos = SystemSettings['repositories']['available']
    for repo in repos:
        if repo in available_repos:
            print_warning("[%s] %s" % (
                purple(repo), blue(_("repository already enabled")),))
            continue
        if repo not in excluded_repos:
            print_warning("[%s] %s" % (
                purple(repo), blue(_("repository not available")),))
            continue
        enabled = entropy_client.enable_repository(repo)
        if enabled:
            print_info("[%s] %s" % (
                    teal(repo), blue(_("repository enabled")),))
        else:
            print_warning("[%s] %s" % (
                    purple(repo), blue(_("cannot enable repository")),))
    return 0

def _disable_repositories(entropy_client, repos):
    excluded_repos = SystemSettings['repositories']['excluded']
    available_repos = SystemSettings['repositories']['available']
    for repo in repos:
        if repo in excluded_repos:
            print_warning("[%s] %s" % (
                purple(repo), blue(_("repository already disabled")),))
            continue
        if repo not in available_repos:
            print_warning("[%s] %s" % (
                purple(repo), blue(_("repository not available")),))
            continue
        disabled = False
        try:
            disabled = entropy_client.disable_repository(repo)
        except ValueError:
            print_warning("[%s] %s" % (
                purple(repo), blue(_("cannot disable repository")),))
            continue
        if disabled:
            print_info("[%s] %s" % (
                    teal(repo), blue(_("repository disabled")),))
        else:
            print_warning("[%s] %s" % (
                    purple(repo), blue(_("cannot disable repository")),))
    return 0

def _merge_repository(entropy_client, repo_ids, remove_conflicts = False):
    if len(repo_ids) < 2:
        print_error("[%s] %s" % (
            purple('x'), blue(_("not enough repositories specified")),))
        return 1

    source_repos, dest_repo = repo_ids[:-1], repo_ids[-1]

    # validate source repos
    available_repos = SystemSettings['repositories']['available']
    not_found = [x for x in source_repos if x not in available_repos]
    if dest_repo not in available_repos:
        not_found.append(dest_repo)
    if not_found:
        print_error("[%s] %s" % (
            purple(', '.join(not_found)),
            blue(_("repositories not found")),))
        return 2

    # source = dest?
    if dest_repo in source_repos:
        print_error("[%s] %s" % (
            purple(dest_repo),
            blue(_("repository cannot be source and destination")),))
        return 3

    print_info("[%s] %s" % (
        teal(', '.join(source_repos)) + "=>" + purple(dest_repo),
        blue(_("merging repositories")),))

    repo_meta = SystemSettings['repositories']['available'][dest_repo]
    repo_path = os.path.join(repo_meta['dbpath'], etpConst['etpdatabasefile'])
    # make sure all the repos are closed
    entropy_client.close_repositories()
    # this way it's open read/write
    dest_db = entropy_client.open_generic_repository(repo_path)

    for source_repo in source_repos:
        print_info("[%s] %s" % (
            teal(source_repo), blue(_("working on repository")),))
        source_db = entropy_client.open_repository(source_repo)
        pkg_ids = source_db.listAllPackageIds(order_by = 'atom')
        total = len(pkg_ids)
        count = 0
        conflict_cache = set()
        for pkg_id in pkg_ids:
            count += 1
            pkg_meta = source_db.getPackageData(pkg_id, get_content = True,
                content_insert_formatted = True)

            print_info("[%s:%s|%s] %s" % (
                purple(str(count)),
                darkgreen(str(total)),
                teal(pkg_meta['atom']), blue(_("merging package")),),
                    back = True)

            target_pkg_ids = dest_db.getPackagesToRemove(
                pkg_meta['name'], pkg_meta['category'],
                pkg_meta['slot'], pkg_meta['injected'])
            if remove_conflicts:
                for conflict in pkg_meta['conflicts']:
                    if conflict in conflict_cache:
                        continue
                    conflict_cache.add(conflict)
                    matches, rc = dest_db.atomMatch(conflict,
                        multiMatch = True)
                    target_pkg_ids |= matches
            for target_pkg_id in target_pkg_ids:
                dest_db.removePackage(target_pkg_id, do_cleanup = False,
                    do_commit = False)
            dest_pkg_id = dest_db.addPackage(pkg_meta,
                do_commit = False, formatted_content = True)

        print_info("[%s] %s" % (
            teal(source_repo), blue(_("done merging packages")),))

    dest_db.commit()
    dest_db.close()
    # close all repos again
    entropy_client.close_repositories()

    return 0

def _mirror_sort(entropy_client, repo_ids):

    for repo_id in repo_ids:
        try:
            repo_data = entropy_client.reorder_mirrors(repo_id,
                dry_run = etpUi['pretend'])
        except KeyError:
            print_warning("[%s] %s" % (
                purple(repo_id), blue(_("repository not available")),))
            continue
        # show new order, this doesn't take into account fallback mirrors
        # which are put at the end of the list by SystemSettings logic.
        mirrors = copy.copy(repo_data['plain_packages'])
        if mirrors and not etpUi['pretend']:
            mirrors.reverse()
            print_info("[%s] %s" % (
                teal(repo_id), darkgreen(_("mirror order:")),))
            count = 0
            for mirror in mirrors:
                count += 1
                print_info("  %d. %s" % (count, brown(mirror),))
        print_info("[%s] %s" % (
            teal(repo_id), blue(_("mirrors sorted successfully")),))
    return 0


def _show_repository_info(entropy_client, reponame):

    repo_number = 0
    for repo in SystemSettings['repositories']['order']:
        repo_number += 1
        if repo == reponame:
            break

    avail_data = SystemSettings['repositories']['available']
    repo_data = avail_data[reponame]

    print_info(blue("#"+str(repo_number))+bold(" "+repo_data['description']))
    if os.path.isfile(repo_data['dbpath']+"/"+etpConst['etpdatabasefile']):
        status = _("active")
    else:
        status = _("never synced")
    print_info( darkgreen("\t%s: %s") % (_("Status"), darkred(status),) )
    urlcount = 0

    for repourl in repo_data['packages'][::-1]:
        urlcount += 1
        print_info( red("\t%s #%s: %s") % (
            _("Packages URL"), urlcount, darkgreen(repourl),) )

    print_info( red("\t%s: %s") % (_("Database URL"),
        darkgreen(repo_data['database']),) )
    print_info( red("\t%s: %s") % (_("Repository name"), bold(reponame),) )
    print_info( red("\t%s: %s") % (_("Repository database path"),
        blue(repo_data['dbpath']),) )
    revision = entropy_client.get_repository(reponame).revision(reponame)
    print_info( red("\t%s: %s") % (_("Repository revision"),
        darkgreen(str(revision)),) )

    return 0

def _do_dbus_sync(repositories, force=False):

    info_txt = \
        _("Sending the update request to Entropy Services")
    info_txt2 = _("Repositories will be updated in background")
    print_info(purple(info_txt) + ".")
    print_info(teal(info_txt2) + ".")

    def bail_out(err):
        print_error(darkred(" @@ ")+brown("%s" % (
            _("sys-apps/entropy-client-services not installed?. Update not allowed, please run as root."),) ))
        if err:
            print_error("%s" % (err,))

    try:
        import glib
        import gobject
        import dbus
        from dbus.mainloop.glib import DBusGMainLoop
    except ImportError as err:
        bail_out(err)
        return 1

    dbus_loop = DBusGMainLoop(set_as_default = True)
    loop = glib.MainLoop()
    gobject.threads_init()

    _entropy_dbus_object = None
    tries = 5
    try:
        _system_bus = dbus.SystemBus(mainloop = dbus_loop)
        _entropy_dbus_object = _system_bus.get_object(
            "org.sabayon.Rigo", "/")
    except dbus.exceptions.DBusException as err:
        bail_out(err)
        return 1

    if _entropy_dbus_object is not None:
        iface = dbus.Interface(_entropy_dbus_object,
            dbus_interface = "org.sabayon.Rigo")
        accepted = False
        try:
            accepted = iface.update_repositories(repositories, force)
        except dbus.exceptions.DBusException as err:
            bail_out(err)
            return 1
        if accepted:
            info_txt = _("Have a nice day")
            print_info(brown(info_txt) + ".")
            return 0
        else:
            info_txt = _("Repositories update not allowed")
            print_error(brown(info_txt) + ".")
            return 1

    bail_out(None)
    return 1


def _do_sync(entropy_client, repo_identifiers = None, force = False):

    if repo_identifiers is None:
        repo_identifiers = list(SystemSettings['repositories']['available'])

    # load repository class
    repo_conf = entropy_client.Settings().get_setting_files_data(
        )['repositories']
    try:
        repo_intf = entropy_client.Repositories(repo_identifiers, force = force)
    except AttributeError:
        print_error(darkred(" * ")+red("%s %s" % (
            _("No repositories specified in"), repo_conf,)))
        return 127
    except Exception as err:
        print_error(darkred(" @@ ")+red("%s: %s" % (
            _("Unhandled exception"), err,)))
        return 2

    def _spawn_ugc():
        for repository in repo_identifiers:
            try:
                webserv = get_entropy_webservice(entropy_client,
                    repository, tx_cb = False)
            except WebService.UnsupportedService:
                continue
            try:
                webserv.add_downloads([repository],
                    clear_available_cache = True)
            except WebService.WebServiceException as err:
                const_debug_write(__name__, repr(err))
                continue

    ugc_th = ParallelTask(_spawn_ugc)
    ugc_th.start()

    rc = repo_intf.sync()
    if not rc:
        for reponame in repo_identifiers:
            _show_notice_board_summary(entropy_client, reponame)

    ugc_th.join()
    return rc

def _check_notice_board_availability(entropy_client, reponame):

    def show_err():
        print_error(darkred(" @@ ")+blue("%s" % (
            _("Notice board not available"),) ))

    try:
        data = entropy_client.get_noticeboard(reponame)
    except KeyError:
        data = None
    if not data:
        show_err()
        return

    return data

def _show_notice(entropy_client, key, mydict):

    mytxt = "[%s] [%s] %s: %s" % (
        blue(str(key)),
        brown(mydict['pubDate']),
        _("Title"),
        darkred(mydict['title']),
    )
    print_info(mytxt)

    mytxt = "\t%s: %s" % (
        darkgreen(_("Content")),
        blue(mydict['description']),
    )
    print_info(mytxt)
    mytxt = "\t%s: %s" % (
        darkgreen(_("Link")),
        blue(mydict['link']),
    )
    print_info(mytxt)

    def fake_callback(s):
        return True

    input_params = [('idx', _('Press Enter to continue'), fake_callback, False)]
    entropy_client.input_box('', input_params, cancel_button = True)
    return


def _show_notice_selector(entropy_client, title, mydict):
    mykeys = sorted(mydict.keys())

    for key in mykeys:
        mydata = mydict.get(key)
        mytxt = "[%s] [%s] %s: %s" % (
            blue(str(key)),
            brown(mydata['pubDate']),
            _("Title"),
            darkred(mydata['title']),
        )
        print_info(mytxt)

    mytxt = "[%s] %s" % (
        blue("-1"),
        darkred(_("Exit")),
    )
    print_info(mytxt)

    def fake_callback(s):
        return s
    input_params = [('id',
        blue(_('Choose one by typing its identifier')), fake_callback, False)]
    data = entropy_client.input_box(title, input_params, cancel_button = True)
    if not isinstance(data, dict):
        return -1
    try:
        return int(data['id'])
    except ValueError:
        return -2

def _notice_board_reader(entropy_client, reponame):

    data = _check_notice_board_availability(entropy_client, reponame)
    if not data:
        return
    counter = len(data)
    while True:
        try:
            sel = _show_notice_selector(entropy_client, '', data)
        except KeyboardInterrupt:
            return 0
        if (sel >= 0) and (sel < counter):
            _show_notice(entropy_client, sel, data.get(sel))
        elif sel == -1:
            return 0


def _show_notice_board_summary(entropy_client, reponame):

    mytxt = "%s %s: %s" % (darkgreen(" @@ "),
        brown(_("Notice board")), bold(reponame),)
    print_info(mytxt)

    mydict = _check_notice_board_availability(entropy_client, reponame)
    if not mydict:
        return

    for key in sorted(mydict):
        mydata = mydict.get(key)
        mytxt = "    [%s] [%s] %s: %s" % (
            blue(str(key)),
            brown(mydata['pubDate']),
            _("Title"),
            darkred(mydata['title']),
        )
        print_info(mytxt)
