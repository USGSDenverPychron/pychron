# ===============================================================================
# Copyright 2013 Jake Ross
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ===============================================================================

# ============= enthought library imports =======================
from __future__ import absolute_import
from __future__ import print_function

import hashlib
import os
import shutil
import subprocess
from datetime import datetime

import time
from git import Repo
from git.exc import GitCommandError
from traits.api import Any, Str, List, Event

from pychron.core.codetools.inspection import caller
from pychron.core.helpers.filetools import fileiter
from pychron.core.progress import open_progress
from pychron.envisage.view_util import open_view
from pychron.git_archive.diff_view import DiffView, DiffModel
from pychron.git_archive.git_objects import GitSha
from pychron.git_archive.merge_view import MergeModel, MergeView
from pychron.git_archive.utils import get_head_commit, ahead_behind
from pychron.git_archive.views import NewBranchView
from pychron.loggable import Loggable
from pychron.pychron_constants import DATE_FORMAT


def get_repository_branch(path):
    r = Repo(path)
    b = r.active_branch
    return b.name


def grep(arg, name):
    process = subprocess.Popen(['grep', '-lr', arg, name], stdout=subprocess.PIPE)
    stdout, stderr = process.communicate()
    return stdout, stderr


def format_date(d):
    return time.strftime("%m/%d/%Y %H:%M", time.gmtime(d))


def isoformat_date(d):
    if isinstance(d, (float, int)):
        d = datetime.fromtimestamp(d)

    return d.strftime('%Y-%m-%d %H:%M:%S')
    # return time.mktime(time.gmtime(d))


class StashCTX(object):
    def __init__(self, repo):
        self._repo = repo

    def __enter__(self):
        self._repo.git.stash()

    def __exit__(self, *args, **kw):
        self._repo.git.stash.pop()


class GitRepoManager(Loggable):
    """
        manage a local git repository

    """

    _repo = Any
    # root=Directory
    path = Str
    selected = Any
    selected_branch = Str
    selected_path_commits = List
    selected_commits = List
    refresh_commits_table_needed = Event
    path_dirty = Event
    remote = Str

    def set_name(self, p):
        self.name = '{}<GitRepo>'.format(os.path.basename(p))

    def open_repo(self, name, root=None):
        """
            name: name of repo
            root: root directory to create new repo
        """
        if root is None:
            p = name
        else:
            p = os.path.join(root, name)

        self.path = p

        self.set_name(p)

        if os.path.isdir(p):
            self.init_repo(p)
            return True
        else:
            os.mkdir(p)
            repo = Repo.init(p)
            self.debug('created new repo {}'.format(p))
            self._repo = repo
            return False

    def init_repo(self, path):
        """
            path: absolute path to repo

            return True if git repo exists
        """
        if os.path.isdir(path):
            g = os.path.join(path, '.git')
            if os.path.isdir(g):
                self._repo = Repo(path)
                self.set_name(path)
                return True
            else:
                self.debug('{} is not a valid repo. Initializing now'.format(path))
                self._repo = Repo.init(path)
                self.set_name(path)

    def delete_local_commits(self, remote='origin', branch='master'):
        self._repo.git.reset('--hard', '{}/{}'.format(remote, branch))

    def add_paths(self, apaths):
        if not isinstance(apaths, (list, tuple)):
            apaths = (apaths,)

        changes = self.get_local_changes()
        changes = [os.path.join(self.path, c) for c in changes]
        if changes:
            self.debug('-------- local changes ---------')
            for c in changes:
                self.debug(c)

        untracked = self.untracked_files()
        if untracked:
            self.debug('-------- untracked paths --------')
            for t in untracked:
                self.debug(t)

        changes.extend(untracked)

        self.debug('add paths {}'.format(apaths))

        ps = [p for p in apaths if p in changes]
        self.debug('changed paths {}'.format(ps))
        changed = bool(ps)
        for p in ps:
            self.debug('adding to index: {}'.format(os.path.relpath(p, self.path)))
        self.index.add(ps)
        return changed

    def add_ignore(self, *args):
        ignores = []
        p = os.path.join(self.path, '.gitignore')
        if os.path.isfile(p):
            with open(p, 'r') as rfile:
                ignores = [line.strip() for line in rfile]

        args = [a for a in args if a not in ignores]
        if args:
            with open(p, 'a') as afile:
                for a in args:
                    afile.write('{}\n'.format(a))
        self.add(p, commit=False)

    def get_modification_date(self, path):
        """
        "Fri May 18 11:13:57 2018 -0600"
        :param path:
        :return:
        """
        d = self.cmd('log', '-1', '--format="%ad"', '--date=format:{}'.format(DATE_FORMAT), '--', path)
        if d:
            d = d[1:-1]
        return d

    def out_of_date(self, branchname='master'):
        pd = open_progress(2)

        repo = self._repo
        origin = repo.remotes.origin
        pd.change_message('Fetching {} {}'.format(origin, branchname))

        repo.git.fetch(origin, branchname)
        pd.change_message('Complete')
        # try:
        #     oref = origin.refs[branchname]
        #     remote_commit = oref.commit
        # except IndexError:
        #     remote_commit = None
        #
        # branch = getattr(repo.heads, branchname)
        # local_commit = branch.commit
        local_commit, remote_commit = self._get_local_remote_commit(branchname)
        self.debug('out of date {} {}'.format(local_commit, remote_commit))
        return local_commit != remote_commit

    def _get_local_remote_commit(self, branchname=None):

        repo = self._repo
        origin = repo.remotes.origin
        try:
            oref = origin.refs[branchname]
            remote_commit = oref.commit
        except IndexError:
            remote_commit = None

        if branchname is None:
            branch = repo.head
        else:
            try:
                branch = repo.heads[branchname]
            except AttributeError:
                return None, None

        local_commit = branch.commit
        return local_commit, remote_commit

    @classmethod
    def clone_from(cls, url, path):
        # progress = open_progress(100)
        #
        # def func(op_code, cur_count, max_count=None, message=''):
        #     if max_count:
        #         progress.max = int(max_count) + 2
        #         if message:
        #             message = 'Cloning repository {} -- {}'.format(url, message[2:])
        #             progress.change_message(message, auto_increment=False)
        #         progress.update(int(cur_count))
        #
        #     if op_code == 66:
        #         progress.close()
        # rprogress = CallableRemoteProgress(func)
        rprogress = None
        try:
            Repo.clone_from(url, path, progress=rprogress)
        except GitCommandError as e:
            print(e)
            shutil.rmtree(path)
            # def foo():
            #     try:
            #         Repo.clone_from(url, path, progress=rprogress)
            #     except GitCommandError:
            #         shutil.rmtree(path)
            #
            #     evt.set()

            # t = Thread(target=foo)
            # t.start()
            # period = 0.1
            # while not evt.is_set():
            #     st = time.time()
            #     # v = prog.get_value()
            #     # if v == n - 2:
            #     #     prog.increase_max(50)
            #     #     n += 50
            #     #
            #     # prog.increment()
            #     time.sleep(max(0, period - time.time() + st))
            # prog.close()

    def clone(self, url, path):
        try:
            self._repo = Repo.clone_from(url, path)
        except GitCommandError as e:
            self.warning_dialog('Cloning error: {}, url={}, path={}'.format(e, url, path))

    def unpack_blob(self, hexsha, p):
        """
            p: str. should be absolute path
        """
        repo = self._repo
        tree = repo.commit(hexsha).tree
        # blob = next((bi for ti in tree.trees
        # for bi in ti.blobs
        # if bi.abspath == p), None)
        blob = None
        for ts in ((tree,), tree.trees):
            for ti in ts:
                for bi in ti.blobs:
                    # print bi.abspath, p
                    if bi.abspath == p:
                        blob = bi
                        break
        else:
            print('failed unpacking', p)

        return blob.data_stream.read() if blob else ''

    def shell(self, cmd, *args):
        repo = self._repo

        func = getattr(repo.git, cmd)
        return func(*args)

    def truncate_repo(self, date='1 month'):
        repo = self._repo
        name = os.path.basename(self.path)
        backup = '.{}'.format(name)
        repo.git.clone('--mirror', ''.format(name), './{}'.format(backup))
        logs = repo.git.log('--pretty=%H', '-after "{}"'.format(date))
        logs = reversed(logs.split('\n'))
        sha = next(logs)

        gpath = os.path.join(self.path, '.git', 'info', 'grafts')
        with open(gpath, 'w') as wfile:
            wfile.write(sha)

        repo.git.filter_branch('--tag-name-filter', 'cat', '--', '--all')
        repo.git.gc('--prune=now')

    def commits_iter(self, p, keys=None, limit='-'):
        repo = self._repo
        p = os.path.join(repo.working_tree_dir, p)

        p = p.replace(' ', '\ ')
        hx = repo.git.log('--pretty=%H', '--follow', '-{}'.format(limit), '--', p).split('\n')

        def func(hi):
            commit = repo.rev_parse(hi)
            r = [hi, ]
            if keys:
                r.extend([getattr(commit, ki) for ki in keys])
            return r

        return (func(ci) for ci in hx)

    def diff(self, a, b):
        repo = self._repo
        return repo.git.diff(a, b, )

    def status(self):
        return self._git_command(self._repo.git.status, 'status')

    def report_local_changes(self):
        self.debug('Local Changes to {}'.format(self.path))
        for p in self.get_local_changes():
            self.debug('\t{}'.format(p))

    def commit_dialog(self):
        from pychron.git_archive.commit_dialog import CommitDialog

        ps = self.get_local_changes()
        cd = CommitDialog(ps)
        info = cd.edit_traits()
        if info.result:
            index = self.index
            index.add([mp.path for mp in cd.valid_paths()])
            self.commit(cd.commit_message)
            return True

    def get_local_changes(self):
        repo = self._repo

        diff = repo.index.diff(None)
        return [di.a_blob.abspath for di in diff.iter_change_type('M')]

        # diff_str = repo.git.diff('HEAD', '--full-index')
        # diff_str = StringIO(diff_str)
        # diff_str.seek(0)
        #
        # class ProcessWrapper:
        #     stderr = None
        #     stdout = None
        #
        #     def __init__(self, f):
        #         self._f = f
        #
        #     def wait(self, *args, **kw):
        #         pass
        #
        #     def read(self):
        #         return self._f.read()
        #
        # proc = ProcessWrapper(diff_str)
        #
        # diff = Diff._index_from_patch_format(repo, proc)
        # root = self.path
        #
        #
        #
        # for diff_added in hcommit.diff('HEAD~1').iter_change_type('A'):
        #     print(diff_added)

        # diff = hcommit.diff()
        # diff = repo.index.diff(repo.head.commit)
        # return [os.path.relpath(di.a_blob.abspath, root) for di in diff.iter_change_type('M')]

        # patches = map(str.strip, diff_str.split('diff --git'))
        # patches = ['\n'.join(p.split('\n')[2:]) for p in patches[1:]]
        #
        # diff_str = StringIO(diff_str)
        # diff_str.seek(0)
        # index = Diff._index_from_patch_format(repo, diff_str)
        #
        # return index, patches
        #

    def get_head_object(self):
        return get_head_commit(self._repo)

    def get_head(self, commit=True, hexsha=True):
        head = self._repo
        if commit:
            head = head.commit()

        if hexsha:
            head = head.hexsha
        return head
        # return self._repo.head.commit.hexsha

    def cmd(self, cmd, *args):
        return getattr(self._repo.git, cmd)(*args)

    def is_dirty(self):
        return self._repo.is_dirty()

    def untracked_files(self):
        lines = self._repo.git.status(porcelain=True,
                                      untracked_files=True)
        # Untracked files preffix in porcelain mode
        prefix = "?? "
        untracked_files = list()
        for line in lines.split('\n'):
            # print 'ffff', line
            if not line.startswith(prefix):
                continue
            filename = line[len(prefix):].rstrip('\n')
            # Special characters are escaped
            if filename[0] == filename[-1] == '"':
                filename = filename[1:-1].decode('string_escape')
            # print 'ffasdfsdf', filename
            untracked_files.append(os.path.join(self.path, filename))
        # finalize_process(proc)
        return untracked_files

    def has_staged(self):
        return self._repo.git.diff('HEAD', '--name-only')
        # return self._repo.is_dirty()

    def has_unpushed_commits(self, remote='origin', branch='master'):
        # return self._repo.git.log('--not', '--remotes', '--oneline')
        return self._repo.git.log('{}/{}..HEAD'.format(remote, branch), '--oneline')

    def add_unstaged(self, root=None, add_all=False, extension=None, use_diff=False):
        if root is None:
            root = self.path

        index = self.index

        def func(ps, extension):
            if extension:
                if not isinstance(extension, tuple):
                    extension = (extension,)
                ps = [pp for pp in ps if os.path.splitext(pp)[1] in extension]

            if ps:
                self.debug('adding to index {}'.format(ps))

                index.add(ps)

        if use_diff:
            pass
            # try:
            # ps = [diff.a_blob.path for diff in index.diff(None)]
            # func(ps, extension)
            # except IOError,e:
            # print 'exception', e
        elif add_all:
            self._repo.get.add('.')
        else:
            for r, ds, fs in os.walk(root):
                ds[:] = [d for d in ds if d[0] != '.']
                ps = [os.path.join(r, fi) for fi in fs]
                func(ps, extension)

    def update_gitignore(self, *args):
        p = os.path.join(self.path, '.gitignore')
        # mode = 'a' if os.path.isfile(p) else 'w'
        args = list(args)
        if os.path.isfile(p):
            with open(p, 'r') as rfile:
                for line in fileiter(rfile, strip=True):
                    for i, ai in enumerate(args):
                        if line == ai:
                            args.pop(i)
        if args:
            with open(p, 'a') as wfile:
                for ai in args:
                    wfile.write('{}\n'.format(ai))
            self._add_to_repo(p, msg='updated .gitignore')

    def get_commit(self, hexsha):
        repo = self._repo
        return repo.commit(hexsha)

    def tag_branch(self, tagname):
        repo = self._repo
        repo.create_tag(tagname)

    def get_current_branch(self):
        repo = self._repo
        return repo.active_branch.name

    def checkout_branch(self, name):
        repo = self._repo
        branch = getattr(repo.heads, name)
        try:
            branch.checkout()
            self.selected_branch = name
            self._load_branch_history()
            self.information_dialog('Repository now on branch "{}"'.format(name))

        except BaseException as e:
            self.warning_dialog('There was an issue trying to checkout branch "{}"'.format(name))
            raise e

    def delete_branch(self, name):
        self._repo.delete_head(name)

    def get_branch(self, name):
        return getattr(self._repo.heads, name)

    def create_branch(self, name=None, commit='HEAD', inform=True):
        repo = self._repo

        if name is None:
            print(repo.branches, type(repo.branches))
            nb = NewBranchView(branches=repo.branches)
            info = nb.edit_traits()
            if info.result:
                name = nb.name
            else:
                return

        if name not in repo.branches:
            branch = repo.create_head(name, commit=commit)
            branch.checkout()
            if inform:
                self.information_dialog('Repository now on branch "{}"'.format(name))
            return True

    def create_remote(self, url, name='origin', force=False):
        repo = self._repo
        if repo:
            self.debug('setting remote {} {}'.format(name, url))
            # only create remote if doesnt exist
            if not hasattr(repo.remotes, name):
                self.debug('create remote {}'.format(name, url))
                repo.create_remote(name, url)
            elif force:
                repo.delete_remote(name)
                repo.create_remote(name, url)

    def delete_remote(self, name='origin'):
        repo = self._repo
        if repo:
            if hasattr(repo.remotes, name):
                repo.delete_remote(name)

    def get_branch_names(self):
        return [b.name for b in self._repo.branches]

    @caller
    def pull(self, branch='master', remote='origin', handled=True, use_progress=True):
        """
            fetch and merge
        """
        self.debug('pulling {} from {}'.format(branch, remote))

        repo = self._repo
        try:
            remote = self._get_remote(remote)
        except AttributeError as e:
            print('repo man pull', e)
            return

        if remote:
            self.debug('pulling from url: {}'.format(remote.url))
            if use_progress:
                prog = open_progress(3,
                                     show_percent=False,
                                     title='Pull Repository {}'.format(self.name), close_at_end=False)
                prog.change_message('Fetching branch:"{}" from "{}"'.format(branch, remote))
            try:
                self.fetch(remote)
            except GitCommandError as e:
                self.debug(e)
                if not handled:
                    raise e
            self.debug('fetch complete')
            # if use_progress:
            #     for i in range(100):
            #         prog.change_message('Merging {}'.format(i))
            #         time.sleep(1)
            try:
                repo.git.merge('FETCH_HEAD')
            except GitCommandError:
                self.smart_pull(branch=branch, remote=remote)

            # self._git_command(lambda: repo.git.merge('FETCH_HEAD'), 'merge')

            if use_progress:
                prog.close()

        self.debug('pull complete')

    def has_remote(self, remote='origin'):
        return bool(self._get_remote(remote))

    def push(self, branch='master', remote=None, inform=False):
        if remote is None:
            remote = 'origin'

        repo = self._repo
        rr = self._get_remote(remote)
        if rr:
            self._git_command(lambda: repo.git.push(remote, branch), tag='GitRepoManager.push')
            if inform:
                self.information_dialog('{} push complete'.format(self.name))
        else:
            self.warning('No remote called "{}"'.format(remote))

    def _git_command(self, func, tag):
        try:
            return func()
        except GitCommandError as e:
            self.warning('Git command failed. {}, {}'.format(e, tag))

    def smart_pull(self, branch='master', remote='origin',
                   quiet=True,
                   accept_our=False, accept_their=False):
        try:
            ahead, behind = self.ahead_behind(remote)
        except GitCommandError as e:
            self.debug('Smart pull error: {}'.format(e))
            return

        self.debug('Smart pull ahead: {} behind: {}'.format(ahead, behind))
        repo = self._repo
        if behind:
            if ahead:
                if not quiet:
                    if not self.confirmation_dialog('You are {} behind and {} commits ahead. '
                                                    'There are potential conflicts that you will have to resolve.'
                                                    'Would you like to Continue?'.format(behind, ahead)):
                        return

                # potentially conflicts
                with StashCTX(repo):
                    # do merge
                    try:
                        repo.git.rebase('--preserve-merges', '{}/{}'.format(remote, branch))
                    except GitCommandError:
                        if self.confirmation_dialog('There appears to be a problem with {}.'
                                                    '\n\nWould you like to accept the master copy'.format(self.name)):
                            try:
                                repo.git.merge('--abort')
                            except GitCommandError:
                                pass

                            repo.git.pull('-X', 'theirs', '--commit', '--no-edit')
                            return True
                        else:
                            return

                # self._git_command(lambda: repo.git.rebase('--preserve-merges',
                #                                           '{}/{}'.format(remote, branch)),
                #                   'GitRepoManager.smart_pull/ahead')
                # try:
                #     repo.git.merge('FETCH_HEAD')
                # except BaseException:
                #     pass

                # get conflicted files
                out, err = grep('<<<<<<<', self.path)
                conflict_paths = [os.path.relpath(x, self.path) for x in out.splitlines()]
                self.debug('conflict_paths: {}'.format(conflict_paths))
                if conflict_paths:
                    mm = MergeModel(conflict_paths,
                                    branch=branch,
                                    remote=remote,
                                    repo=self)
                    if accept_our:
                        mm.accept_our()
                    elif accept_their:
                        mm.accept_their()
                    else:
                        mv = MergeView(model=mm)
                        mv.edit_traits()
                else:
                    if not quiet:
                        self.information_dialog('There were no conflicts identified')
            else:
                self.debug('merging {} commits'.format(behind))
                self._git_command(lambda: repo.git.merge('FETCH_HEAD'), 'GitRepoManager.smart_pull/!ahead')
                # repo.git.merge('FETCH_HEAD')
        else:
            self.debug('Up-to-date with {}'.format(remote))
            if not quiet:
                self.information_dialog('Up-to-date with {}'.format(remote))

        return True

    def fetch(self, remote='origin'):
        if self._repo:
            return self._git_command(lambda: self._repo.git.fetch(remote), 'GitRepoManager.fetch')
            # return self._repo.git.fetch(remote)

    def ahead_behind(self, remote='origin'):
        self.debug('ahead behind')

        repo = self._repo
        ahead, behind = ahead_behind(repo, remote)

        return ahead, behind

    def merge(self, src, dest):
        repo = self._repo
        dest = getattr(repo.branches, dest)
        dest.checkout()

        src = getattr(repo.branches, src)
        # repo.git.merge(src.commit)
        self._git_command(lambda: repo.git.merge(src.commit), 'GitRepoManager.merge')

    def commit(self, msg):
        self.debug('commit message={}'.format(msg))
        index = self.index
        if index:
            index.commit(msg)

    def add(self, p, msg=None, msg_prefix=None, verbose=True, **kw):
        repo = self._repo
        # try:
        #     n = len(repo.untracked_files)
        # except IOError:
        #     n = 0

        # try:
        #     if not repo.is_dirty() and not n:
        #         return
        # except OSError:
        #     pass

        bp = os.path.basename(p)
        dest = os.path.join(repo.working_dir, p)

        dest_exists = os.path.isfile(dest)
        if msg_prefix is None:
            msg_prefix = 'modified' if dest_exists else 'added'

        if not dest_exists:
            self.debug('copying to destination.{}>>{}'.format(p, dest))
            shutil.copyfile(p, dest)

        if msg is None:
            msg = '{}'.format(bp)
        msg = '{} - {}'.format(msg_prefix, msg)
        if verbose:
            self.debug('add to repo msg={}'.format(msg))

        self._add_to_repo(dest, msg, **kw)

    def get_log(self, branch, *args):
        if branch is None:
            branch = self._repo.active_branch

        # repo = self._repo
        # l = repo.active_branch.log(*args)
        l = self.cmd('log', branch, '--oneline', *args)
        return l.split('\n')

    def get_active_branch(self):
        return self._repo.active_branch.name

    def get_sha(self, path=None):
        sha = ''
        if path:
            l = self.cmd('ls-tree', 'HEAD', path)
            try:
                mode, kind, sha_name = l.split(' ')
                sha, name = sha_name.split('\t')
            except ValueError:
                pass

        return sha

    def add_tag(self, name, message, hexsha=None):
        args = ('-a', name, '-m', message)
        if hexsha:
            args = args + (hexsha,)

        self.cmd('tag', *args)

    # action handlers
    def diff_selected(self):
        if self._validate_diff():
            if len(self.selected_commits) == 2:
                l, r = self.selected_commits
                dv = self._diff_view_factory(l, r)
                open_view(dv)

    def revert_to_selected(self):
        # check for uncommitted changes
        # warn user the uncommitted changes will be lost if revert now

        commit = self.selected_commits[-1]
        self.revert(commit.hexsha, self.selected)

    def revert(self, hexsha, path):
        self._repo.git.checkout(hexsha, path)
        self.path_dirty = path
        self._set_active_commit()

    def load_file_history(self, p):
        repo = self._repo
        try:
            hexshas = repo.git.log('--pretty=%H', '--follow', '--', p).split('\n')

            self.selected_path_commits = self._parse_commits(hexshas, p)
            self._set_active_commit()

        except GitCommandError:
            self.selected_path_commits = []

    # private
    def _validate_diff(self):
        return True

    def _diff_view_factory(self, a, b):
        # d = self.diff(a.hexsha, b.hexsha)
        if not a.blob:
            a.blob = self.unpack_blob(a.hexsha, a.name)

        if not b.blob:
            b.blob = self.unpack_blob(b.hexsha, b.name)

        model = DiffModel(left_text=b.blob, right_text=a.blob)
        dv = DiffView(model=model)
        return dv

    def _add_to_repo(self, p, msg, commit=True):
        index = self.index
        if index:
            if not isinstance(p, list):
                p = [p]
            try:
                index.add(p)
            except IOError as e:
                self.warning('Failed to add file. Error:"{}"'.format(e))

                # an IOError has been caused in the past by "'...index.lock' could not be obtained"
                os.remove(os.path.join(self.path, '.git', 'index.lock'))
                try:
                    self.warning('Retry after "Failed to add file"'.format(e))
                    index.add(p)
                except IOError as e:
                    self.warning('Retry failed. Error:"{}"'.format(e))
                    return

            if commit:
                index.commit(msg)

    def _get_remote(self, remote):
        repo = self._repo
        try:
            return getattr(repo.remotes, remote)
        except AttributeError:
            pass

    def _get_branch_history(self):
        repo = self._repo
        hexshas = repo.git.log('--pretty=%H').split('\n')
        return hexshas

    def _load_branch_history(self):
        hexshas = self._get_branch_history()
        self.commits = self._parse_commits(hexshas)

    def _parse_commits(self, hexshas, p=''):
        def factory(ci):
            repo = self._repo
            obj = repo.rev_parse(ci)
            cx = GitSha(message=obj.message,
                        hexsha=obj.hexsha,
                        name=p,
                        date=format_date(obj.committed_date))
            return cx

        return [factory(ci) for ci in hexshas]

    def _set_active_commit(self):
        p = self.selected
        with open(p, 'r') as rfile:
            chexsha = hashlib.sha1(rfile.read()).hexdigest()

        for c in self.selected_path_commits:
            blob = self.unpack_blob(c.hexsha, p)
            c.active = chexsha == hashlib.sha1(blob).hexdigest() if blob else False

        self.refresh_commits_table_needed = True

    # handlers
    def _selected_fired(self, new):
        if new:
            self._selected_hook(new)
            self.load_file_history(new)

    def _selected_hook(self, new):
        pass

    def _remote_changed(self, new):
        if new:
            self.delete_remote()
            r = 'https://github.com/{}'.format(new)
            self.create_remote(r)

    @property
    def index(self):
        return self._repo.index

    @property
    def active_repo(self):
        return self._repo


if __name__ == '__main__':
    repo = GitRepoManager()
    repo.open_repo('/Users/ross/Sandbox/mergetest/blocal')
    repo.smart_pull()

    # rp = GitRepoManager()
    # rp.init_repo('/Users/ross/Pychrondata_dev/scripts')
    # rp.commit_dialog()

    # ============= EOF =============================================
    # repo manager protocol
    # def get_local_changes(self, repo=None):
    # repo = self._get_repo(repo)
    #     diff_str = repo.git.diff('--full-index')
    #     patches = map(str.strip, diff_str.split('diff --git'))
    #     patches = ['\n'.join(p.split('\n')[2:]) for p in patches[1:]]
    #
    #     diff_str = StringIO(diff_str)
    #     diff_str.seek(0)
    #     index = Diff._index_from_patch_format(repo, diff_str)
    #
    #     return index, patches
    # def is_dirty(self, repo=None):
    #     repo = self._get_repo(repo)
    #     return repo.is_dirty()
    # def get_untracked(self):
    #     return self._repo.untracked_files
    #     def _add_repo(self, root):
    # existed=True
    # if not os.path.isdir(root):
    #     os.mkdir(root)
    #     existed=False
    #
    # gitdir=os.path.join(root, '.git')
    # if not os.path.isdir(gitdir):
    #     repo = Repo.init(root)
    #     existed = False
    # else:
    #     repo = Repo(root)
    #
    # return repo, existed

    # def add_repo(self, localpath):
    #     """
    #         add a blank repo at ``localpath``
    #     """
    #     repo, existed=self._add_repo(localpath)
    #     self._repo=repo
    #     self.root=localpath
    #     return existed
