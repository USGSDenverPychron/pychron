# ===============================================================================
# Copyright 2015 Jake Ross
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
from traits.api import Instance
# ============= standard library imports ========================
import json
import os
import time
from itertools import groupby
from datetime import timedelta
# ============= local library imports  ==========================
from numpy import array, array_split
from pychron.canvas.utils import make_geom
from pychron.core.helpers.datetime_tools import make_timef, bin_timestamps, get_datetime
from pychron.database.isotope_database_manager import IsotopeDatabaseManager
from pychron.database.records.isotope_record import IsotopeRecordView
from pychron.dvc import dvc_dump
from pychron.dvc.dvc import DVC
from pychron.dvc.dvc_persister import DVCPersister, format_repository_identifier
from pychron.experiment.automated_run.persistence_spec import PersistenceSpec
from pychron.experiment.automated_run.spec import AutomatedRunSpec
from pychron.experiment.utilities.identifier import make_runid, IDENTIFIER_REGEX, SPECIAL_IDENTIFIER_REGEX
from pychron.git_archive.repo_manager import GitRepoManager
from pychron.github import Organization
from pychron.loggable import Loggable
from pychron.paths import paths
from pychron.pychron_constants import ALPHAS

ORG = 'NMGRLData'


def create_github_repo(name):
    org = Organization(ORG)
    if not org.has_repo(name):
        usr = os.environ.get('GITHUB_USER')
        pwd = os.environ.get('GITHUB_PWD')
        org.create_repo(name, usr, pwd)


class IsoDBTransfer(Loggable):
    """
    transfer analyses from an isotope_db database to a dvc database
    """
    dvc = Instance(DVC)
    processor = Instance(IsotopeDatabaseManager)
    persister = Instance(DVCPersister)

    quiet = False

    def init(self):
        conn = dict(host=os.environ.get('ARGONSERVER_HOST'),
                    username=os.environ.get('ARGONSERVER_DB_USER'),
                    password=os.environ.get('ARGONSERVER_DB_PWD'),
                    kind='mysql')

        self.dvc = DVC(bind=False,
                       organization='NMGRLData',
                       meta_repo_name='MetaData')
        paths.meta_root = os.path.join(paths.dvc_dir, self.dvc.meta_repo_name)

        use_local = True
        if use_local:
            dest_conn = dict(host='localhost',
                             username=os.environ.get('LOCALHOST_DB_USER'),
                             password=os.environ.get('LOCALHOST_DB_PWD'),
                             kind='mysql',
                             # echo=True,
                             name='pychrondvc_dev')
        else:
            dest_conn = conn.copy()
            dest_conn['name'] = 'pychrondvc'

        self.dvc.db.trait_set(**dest_conn)
        if not self.dvc.initialize():
            self.warning_dialog('Failed to initialize DVC')
            return

        self.dvc.meta_repo.smart_pull(quiet=self.quiet)
        self.persister = DVCPersister(dvc=self.dvc, stage_files=False)

        proc = IsotopeDatabaseManager(bind=False, connect=False)

        use_local_src = True
        if use_local_src:
            conn = dict(host='localhost',
                        username=os.environ.get('LOCALHOST_DB_USER'),
                        password=os.environ.get('LOCALHOST_DB_PWD'),
                        kind='mysql',
                        # echo=True,
                        name='pychrondata_dev')
        else:
            conn['name'] = 'pychrondata'

        proc.db.trait_set(**conn)
        src = proc.db
        src.connect()
        self.processor = proc

    def bulk_import_irradiations(self, creator, dry=True):

        # for i in xrange(251, 277):
        # for i in xrange(258, 259):
        # for i in (258, 259, 260, 261,):
        # for i in (262, 263, 264, 265):
        # for i in (266, 267, 268, 269):
        # for i in (270, 271, 272, 273):
        for i in (273,):
            irradname = 'NM-{}'.format(i)
            runs = self.bulk_import_irradiation(irradname, creator, dry=dry)
            # if runs:
            #     with open('/Users/ross/Sandbox/bulkimport/irradiation_runs.txt', 'a') as wfile:
            #         for o in runs:
            #             wfile.write('{}\n'.format(o))

    def bulk_import_irradiation(self, irradname, creator, dry=True):

        src = self.processor.db
        tol_hrs = 6
        self.debug('bulk import irradiation {}'.format(irradname))
        oruns = []
        ts, idxs = self._get_irradiation_timestamps(irradname, tol_hrs=tol_hrs)
        repository_identifier = 'Irradiation-{}'.format(irradname)

        def filterfunc(x):
            a = x.labnumber.irradiation_position is None
            b = False
            if not a:
                b = x.labnumber.irradiation_position.level.irradiation.name == irradname

            d = False
            if x.extraction:
                ed = x.extraction.extraction_device
                if not ed:
                    d = True
                else:
                    d = ed.name == 'Fusions CO2'

            return (a or b) and d

        # for ms in ('jan', 'obama'):

        # monitors not run on obama
        for ms in ('jan',):
            for i, ais in enumerate(array_split(ts, idxs + 1)):
                if not ais.shape[0]:
                    self.debug('skipping {}'.format(i))
                    continue

                low = get_datetime(ais[0]) - timedelta(hours=tol_hrs / 2.)
                high = get_datetime(ais[-1]) + timedelta(hours=tol_hrs / 2.)
                with src.session_ctx():
                    ans = src.get_analyses_date_range(low, high,
                                                      mass_spectrometers=(ms,),
                                                      samples=('FC-2',
                                                               'blank_unknown', 'blank_air', 'blank_cocktail', 'air',
                                                               'cocktail'))

                    # runs = filter(lambda x: x.labnumber.irradiation_position is None or
                    #                         x.labnumber.irradiation_position.level.irradiation.name == irradname, ans)

                    runs = filter(filterfunc, ans)
                    if dry:
                        for ai in runs:
                            oruns.append(ai.record_id)
                            print ms, ai.record_id
                    else:
                        self.debug('================= Do Export i: {} low: {} high: {}'.format(i, low, high))
                        self.debug('N runs: {}'.format(len(runs)))
                        self.do_export([ai.record_id for ai in runs], repository_identifier, creator)

        return oruns

    def bulk_import_project(self, project, principal_investigator, dry=True):
        src = self.processor.db
        tol_hrs = 6
        self.debug('bulk import project={}, pi={}'.format(project, principal_investigator))
        oruns = []
        ts, idxs = self._get_project_timestamps(project, tol_hrs=tol_hrs)
        repository_identifier = project

        # def filterfunc(x):
        #     a = x.labnumber.irradiation_position is None
        #     b = False
        #     if not a:
        #         b = x.labnumber.irradiation_position.level.irradiation.name == irradname
        #
        #     d = False
        #     if x.extraction:
        #         ed = x.extraction.extraction_device
        #         if not ed:
        #             d = True
        #         else:
        #             d = ed.name == 'Fusions CO2'
        #
        #     return (a or b) and d
        #
        for ms in ('jan', 'obama'):
            for i, ais in enumerate(array_split(ts, idxs + 1)):
                if not ais.shape[0]:
                    self.debug('skipping {}'.format(i))
                    continue

                low = get_datetime(ais[0]) - timedelta(hours=tol_hrs / 2.)
                high = get_datetime(ais[-1]) + timedelta(hours=tol_hrs / 2.)

                print ms, low, high
        # with src.session_ctx():
        #             ans = src.get_analyses_date_range(low, high,
        #                                               mass_spectrometers=(ms,))
        #
        #             # runs = filter(lambda x: x.labnumber.irradiation_position is None or
        #             #                         x.labnumber.irradiation_position.level.irradiation.name == irradname, ans)
        #
        #             runs = filter(filterfunc, ans)
        #             if dry:
        #                 for ai in runs:
        #                     oruns.append(ai.record_id)
        #                     print ms, ai.record_id
        #             else:
        #                 self.debug('================= Do Export i: {} low: {} high: {}'.format(i, low, high))
        #                 self.debug('N runs: {}'.format(len(runs)))
        #                 self.do_export([ai.record_id for ai in runs], repository_identifier, principal_investigator)

        return oruns

    def import_date_range(self, low, high, spectrometer, repository_identifier, creator):
        src = self.processor.db
        with src.session_ctx():
            runs = src.get_analyses_date_range(low, high, mass_spectrometers=spectrometer)

            ais = [ai.record_id for ai in runs]
        self.do_export(ais, repository_identifier, creator)

    def do_export(self, runs, repository_identifier, creator, create_repo=False):

        # self._init_src_dest()
        src = self.processor.db
        dest = self.dvc.db

        with src.session_ctx():
            key = lambda x: x.split('-')[0]
            runs = sorted(runs, key=key)
            with dest.session_ctx():
                repo = self._add_repository(dest, repository_identifier, creator, create_repo)

            self.persister.active_repository = repo
            self.dvc.current_repository = repo

            total = len(runs)
            j = 0

            for ln, ans in groupby(runs, key=key):
                ans = list(ans)
                n = len(ans)
                for i, a in enumerate(ans):
                    with dest.session_ctx() as sess:
                        st = time.time()
                        try:
                            if self._transfer_analysis(a, repository_identifier):
                                j += 1
                                self.debug('{}/{} transfer time {:0.3f}'.format(j, total, time.time() - st))
                        except BaseException, e:
                            import traceback
                            traceback.print_exc()
                            self.warning('failed transfering {}. {}'.format(a, e))

    def runlist_load(self, path):
        with open(path, 'r') as rfile:
            runs = [li.strip() for li in rfile]
            # runs = [line.strip() for line in rfile if line.strip()]
            return filter(None, runs)

    def runlist_loads(self, txt):
        runs = [li.strip() for li in txt.striplines()]
        return filter(None, runs)

    # private
    def _get_project_timestamps(self, project, tol_hrs=6):
        src = self.processor.db
        with src.session_ctx() as sess:
            sql = """SELECT ant.analysis_timestamp from meas_analysistable as ant
join gen_labtable as lt on lt.id = ant.lab_id
join gen_sampletable as st on lt.sample_id = st.id
join gen_projecttable as pt on st.project_id = pt.id
where pt.name="{}"
order by ant.analysis_timestamp ASC
""".format(project)

            result = sess.execute(sql)
            ts = array([make_timef(ri[0]) for ri in result.fetchall()])

            idxs = bin_timestamps(ts, tol_hrs=tol_hrs)
            return ts, idxs

    def _get_irradiation_timestamps(self, irradname, tol_hrs=6):
        src = self.processor.db
        with src.session_ctx() as sess:
            sql = """SELECT ant.analysis_timestamp from meas_analysistable as ant
join gen_labtable as lt on lt.id = ant.lab_id
join gen_sampletable as st on lt.sample_id = st.id
join irrad_PositionTable as irp on lt.irradiation_id = irp.id
join irrad_leveltable as il on irp.level_id = il.id
join irrad_irradiationtable as ir on il.irradiation_id = ir.id

where ir.name = "{}" and st.name ="FC-2"
order by ant.analysis_timestamp ASC

""".format(irradname)

            result = sess.execute(sql)
            ts = array([make_timef(ri[0]) for ri in result.fetchall()])

            idxs = bin_timestamps(ts, tol_hrs=tol_hrs)
            return ts, idxs

    def _add_repository(self, dest, repository_identifier, creator, create_repo):
        repository_identifier = format_repository_identifier(repository_identifier)

        # sys.exit()
        proot = os.path.join(paths.repository_dataset_dir, repository_identifier)
        if not os.path.isdir(proot):
            # create new local repo
            os.mkdir(proot)

            repo = GitRepoManager()
            repo.open_repo(proot)

            repo.add_ignore('.DS_Store')
            self.repo_man = repo
            if create_repo:
                # add repo to central location
                create_github_repo(repository_identifier)

                url = 'https://github.com/{}/{}.git'.format(ORG, repository_identifier)
                self.debug('Create repo at github. url={}'.format(url))
                repo.create_remote(url)
        else:
            repo = GitRepoManager()
            repo.open_repo(proot)

        dbexp = dest.get_repository(repository_identifier)
        if not dbexp:
            dest.add_repository(repository_identifier, creator)

        return repo

    def _transfer_meta(self, dest, dban):
        self.debug('transfer meta')

        dblab = dban.labnumber
        dbsam = dblab.sample
        project = dbsam.project.name
        project = project.replace('/', '_').replace('\\', '_')

        sam = dest.get_sample(dbsam.name, project, dbsam.material.name)
        if not sam:
            mat = dbsam.material.name
            if not dest.get_material(mat):
                self.debug('add material {}'.format(mat))
                dest.add_material(mat)
                dest.flush()

            if not dest.get_project(project):
                self.debug('add project {}'.format(project))
                dest.add_project(project)
                dest.flush()

            self.debug('add sample {}'.format(dbsam.name))
            sam = dest.add_sample(dbsam.name, project, mat)
            dest.flush()

        dbirradpos = dblab.irradiation_position
        if not dbirradpos:
            irradname = 'NoIrradiation'
            levelname = 'A'
            holder = 'Grid'
            pos = None
            identifier = dblab.identifier
            doses = []
            prod = None
            prodname = 'NoIrradiation'

            geom = make_geom([(0, 0, 0.0175),
                              (1, 0, 0.0175),
                              (2, 0, 0.0175),
                              (3, 0, 0.0175),
                              (4, 0, 0.0175),

                              (0, 1, 0.0175),
                              (1, 1, 0.0175),
                              (2, 1, 0.0175),
                              (3, 1, 0.0175),
                              (4, 1, 0.0175),

                              (0, 2, 0.0175),
                              (1, 2, 0.0175),
                              (2, 2, 0.0175),
                              (3, 2, 0.0175),
                              (4, 2, 0.0175),

                              (0, 3, 0.0175),
                              (1, 3, 0.0175),
                              (2, 3, 0.0175),
                              (3, 3, 0.0175),
                              (4, 3, 0.0175),

                              (0, 4, 0.0175),
                              (1, 4, 0.0175),
                              (2, 4, 0.0175),
                              (3, 4, 0.0175),
                              (4, 4, 0.0175)
                              ])
        else:
            dblevel = dbirradpos.level
            dbirrad = dblevel.irradiation
            dbchron = dbirrad.chronology

            irradname = dbirrad.name
            levelname = dblevel.name

            holder = dblevel.holder.name if dblevel.holder else ''
            geom = dblevel.holder.geometry if dblevel.holder else ''
            prodname = dblevel.production.name if dblevel.production else ''
            prodname = prodname.replace(' ', '_')
            pos = dbirradpos.position
            doses = dbchron.get_doses()

        meta_repo = self.dvc.meta_repo
        # save db irradiation
        if not dest.get_irradiation(irradname):
            self.debug('Add irradiation {}'.format(irradname))

            self.dvc.add_irradiation(irradname, doses)
            dest.flush()
            # meta_repo.add_irradiation(irradname)
            # meta_repo.add_chronology(irradname, doses, add=False)
            # meta_repo.commit('added irradiation {}'.format(irradname))

        # save production name to db
        if not dest.get_production(prodname):
            self.debug('Add production {}'.format(prodname))
            dest.add_production(prodname)
            dest.flush()

            # meta_repo.add_production(irradname, prodname, prod, add=False)
            # meta_repo.commit('added production {}'.format(prodname))

        # save db level
        if not dest.get_irradiation_level(irradname, levelname):
            self.debug('Add level irrad:{} level:{}'.format(irradname, levelname))
            dest.add_irradiation_level(levelname, irradname, holder, prodname)
            dest.flush()

            meta_repo.add_irradiation_holder(holder, geom, add=False)
            meta_repo.add_level(irradname, levelname, add=False)
            meta_repo.update_level_production(irradname, levelname, prodname)

            # meta_repo.commit('added empty level {}{}'.format(irradname, levelname))

        if pos is None:
            pos = self._get_irradpos(dest, irradname, levelname, identifier)

        # save db irradiation position
        if not dest.get_irradiation_position(irradname, levelname, pos):
            self.debug('Add position irrad:{} level:{} pos:{}'.format(irradname, levelname, pos))
            p = meta_repo.get_level_path(irradname, levelname)
            with open(p, 'r') as rfile:
                yd = json.load(rfile)

            dd = dest.add_irradiation_position(irradname, levelname, pos)
            dd.identifier = dblab.identifier
            dd.sample = sam
            dest.flush()
            try:
                f = dban.labnumber.selected_flux_history.flux
                j, e = f.j, f.j_err
            except AttributeError:
                j, e = 0, 0

            yd.append({'j': j, 'j_err': e, 'position': pos, 'decay_constants': {}})
            dvc_dump(yd, p)

        dest.commit()

    def _transfer_analysis(self, rec, exp, overwrite=True):
        dest = self.dvc.db
        proc = self.processor
        src = proc.db

        # args = rec.split('-')
        # idn = '-'.join(args[:-1])
        # t = args[-1]
        # try:
        #     aliquot = int(t)
        #     step = None
        # except ValueError:
        #     aliquot = int(t[:-1])
        #     step = t[-1]
        m = IDENTIFIER_REGEX.match(rec)
        if not m:
            m = SPECIAL_IDENTIFIER_REGEX.match(rec)

        if not m:
            self.warning('invalid runid {}'.format(rec))
            return
        else:
            idn = m.group('identifier')
            aliquot = m.group('aliquot')
            try:
                step = m.group('step') or None
            except IndexError:
                step = None

        if idn == '4359':
            idn = 'c-01-j'
        elif idn == '4358':
            idn = 'c-01-o'

        # check if analysis already exists. skip if it does
        if dest.get_analysis_runid(idn, aliquot, step):
            self.warning('{} already exists'.format(make_runid(idn, aliquot, step)))
            return

        dban = src.get_analysis_runid(idn, aliquot, step)
        iv = IsotopeRecordView()
        iv.uuid = dban.uuid

        self.debug('make analysis idn:{}, aliquot:{} step:{}'.format(idn, aliquot, step))
        try:
            an = proc.make_analysis(iv, unpack=True, use_cache=False)
        except:
            self.warning('Failed to make {}'.format(make_runid(idn, aliquot, step)))
            return

        self._transfer_meta(dest, dban)
        # return

        dblab = dban.labnumber
        dbsam = dblab.sample

        if dblab.irradiation_position:
            irrad = dblab.irradiation_position.level.irradiation.name
            level = dblab.irradiation_position.level.name
            irradpos = dblab.irradiation_position.position
        else:
            irrad = 'NoIrradiation'
            level = 'A'
            irradpos = self._get_irradpos(dest, irrad, level, dblab.identifier)
            # irrad, level, irradpos = '', '', 0

        sample = dbsam.name
        mat = dbsam.material.name
        project = format_repository_identifier(dbsam.project.name)
        extraction = dban.extraction
        ms = dban.measurement.mass_spectrometer.name
        if not dest.get_mass_spectrometer(ms):
            self.debug('adding mass spectrometer {}'.format(ms))
            dest.add_mass_spectrometer(ms)
            dest.commit()

        ed = extraction.extraction_device.name if extraction.extraction_device else None
        if not ed:
            ed = 'No Extract Device'

        if not dest.get_extraction_device(ed):
            self.debug('adding extract device {}'.format(ed))
            dest.add_extraction_device(ed)
            dest.commit()

        if step is None:
            inc = -1
        else:
            inc = ALPHAS.index(step)

        username = ''
        if dban.user:
            username = dban.user.name
            if not dest.get_user(username):
                self.debug('adding user. username:{}'.format(username))
                dest.add_user(username)
                dest.commit()

        rs = AutomatedRunSpec(labnumber=idn,
                              username=username,
                              material=mat,
                              project=project,
                              sample=sample,
                              irradiation=irrad,
                              irradiation_level=level,
                              irradiation_position=irradpos,
                              repository_identifier=exp,
                              mass_spectrometer=ms,
                              uuid=dban.uuid,
                              _step=inc,
                              comment=dban.comment or '',
                              aliquot=int(aliquot),
                              extract_device=ed,
                              duration=extraction.extract_duration,
                              cleanup=extraction.cleanup_duration,
                              beam_diameter=extraction.beam_diameter,
                              extract_units=extraction.extract_units or '',
                              extract_value=extraction.extract_value,
                              pattern=extraction.pattern or '',
                              weight=extraction.weight,
                              ramp_duration=extraction.ramp_duration or 0,
                              ramp_rate=extraction.ramp_rate or 0,

                              collection_version='0.1:0.1',
                              queue_conditionals_name='',
                              tray='')

        ps = PersistenceSpec(run_spec=rs,
                             tag=an.tag.name,
                             arar_age=an,
                             timestamp=dban.analysis_timestamp,
                             use_repository_association=True,
                             positions=[p.position for p in extraction.positions])

        self.debug('transfer analysis with persister')
        self.persister.per_spec_save(ps, commit=False, msg_prefix='Database Transfer')
        return True

    def _get_irradpos(self, dest, irradname, levelname, identifier):
        dl = dest.get_irradiation_level(irradname, levelname)
        pos = 1
        if dl.positions:
            for p in dl.positions:
                if p.identifier == identifier:
                    pos = p.position
                    break
            else:
                pos = dl.positions[-1].position + 1

        return pos


def experiment_id_modifier(root, expid):
    for r, ds, fs in os.walk(root, topdown=True):
        fs = [f for f in fs if not f[0] == '.']
        ds[:] = [d for d in ds if not d[0] == '.']

        # print 'fff',r, os.path.basename(r)
        if os.path.basename(r) in ('intercepts', 'blanks', '.git',
                                   'baselines', 'icfactors', 'extraction', 'tags', '.data', 'monitor', 'peakcenter'):
            continue
        # dcnt+=1
        for fi in fs:
            # if not fi.endswith('.py') or fi == '__init__.py':
            #     continue
            # cnt+=1
            p = os.path.join(r, fi)
            # if os.path.basename(os.path.dirname(p)) =
            print p
            write = False
            with open(p, 'r') as rfile:
                jd = json.load(rfile)
                if 'repository_identifier' in jd:
                    jd['repository_identifier'] = expid
                    write = True

            if write:
                dvc_dump(jd, p)


def load_path():
    # path = '/Users/ross/Sandbox/dvc_imports/NM-276.txt'
    # expid = 'Irradiation-NM-276'
    # creator = 'mcintosh'

    path = '/Users/ross/Sandbox/dvc_imports/chesner_unknowns.txt'
    expid = 'toba'
    creator = 'root'

    runs = e.runlist_load(path)
    return runs, expid, creator


def load_import_request():
    import pymysql.cursors
    # Connect to the database
    connection = pymysql.connect(host='localhost',
                                 user=os.environ.get('DB_USER'),
                                 passwd=os.environ.get('DB_PWD'),
                                 db='labspy',
                                 cursorclass=pymysql.cursors.DictCursor)

    try:
        # connection is not autocommit by default. So you must commit to save
        # your changes.
        # connection.commit()

        with connection.cursor() as cursor:
            # Read a single record
            # sql = "SELECT `id`, `password` FROM `users` WHERE `email`=%s"
            # cursor.execute(sql, ('webmaster@python.org',))
            sql = '''SELECT * FROM importer_importrequest'''
            cursor.execute(sql)
            result = cursor.fetchone()

            runs = result['runlist_blob']
            expid = result['repository_identifier']
            creator = result['requestor_name']

            return runs, expid, creator
    finally:
        connection.close()


if __name__ == '__main__':
    from pychron.core.helpers.logger_setup import logging_setup

    paths.build('_dev')
    logging_setup('de', root=os.path.join(os.path.expanduser('~'), 'Desktop', 'logs'))

    e = IsoDBTransfer()
    e.quiet = True
    e.init()

    # runs, expid, creator = load_path()
    # runs, expid, creator = load_import_request()
    e.bulk_import_irradiations('NMGRL', dry=False)
    e.bulk_import_project('')

    # e.bulk_import_irradiation('NM-274', 'root', dry=False)
    # e.import_date_range('2015-12-07 12:00:43', '2015-12-09 13:45:51', 'jan',
    #                     'MATT_AGU', 'root')
    # e.do_export(runs, expid, creator, create_repo=False)

    # experiment_id_modifier('/Users/ross/Pychron_dev/data/.dvc/experiments/Irradiation-NM-274', 'Irradiation-NM-276')

    # create_github_repo('Irradiation-NM-272')
    # exp = 'J-Curve'
    # url = 'https://github.com/{}/{}.git'.format(org.name, exp)
    # # e.transfer_holder('40_no_spokes')
    # # e.transfer_holder('40_hole')
    # # e.transfer_holder('24_hole')
    #
    # path = '/Users/ross/Sandbox/dvc_imports/NM-275.txt'
    # expid = 'Irradiation-NM-275'
    # creator = 'mcintosh'
    # e.do_export(path, expid, creator, create_repo=False)

    # e.do_export_monitors(path, expid, creator, create_repo=False)
    # e.check_experiment(path, expid)
    # e.do_export(path, expid, creator, create_repo=False)
    # e.export_production('Triga PR 275', db=False)
    # ============= EOF =============================================

