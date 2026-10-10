"""Original generator parity, plugin lifecycle and task pipeline, all offline."""
import ast
from dataclasses import replace
import hashlib
import importlib.util
import itertools
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import warnings

import test_jobs
from hpc_mcp.applications import ApplicationService, ApplicationError, schema_check
from hpc_mcp.jobs import file_manifest

ROOT = Path(__file__).resolve().parents[1]


def reference(name, function):
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', SyntaxWarning)
        tree = ast.parse((ROOT / 'used-scripts' / name).read_text())
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == function)
    namespace = {'Path': Path}
    exec(compile(ast.Module(body=[node], type_ignores=[]), name, 'exec'), namespace)
    return namespace


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_jobs.JobLifecycleTests(); self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.jobs = self.fixture.jobs; self.apps = ApplicationService(self.jobs)
        self.card = self.fixture.source / 'water.gjf'; self.card.write_text('# hf/sto-3g\n\nwater\n\n0 1\nH 0 0 0\n\n')
        self.bundle = self.fixture.root / 'bundle'

    def bundle_for(self, app='gaussian', cluster='lsf'):
        if self.bundle.exists(): shutil.rmtree(self.bundle)
        shutil.copytree(ROOT / 'examples/applications' / app, self.bundle)
        manifest = json.loads((self.bundle / 'manifest.json').read_text()); manifest['clusters'] = [cluster]
        (self.bundle / 'manifest.json').write_text(json.dumps(manifest))
        return self.bundle

    def register(self, app='gaussian', cluster='lsf'):
        record = self.apps.install(str(self.bundle_for(app, cluster)))['application']
        self.apps.check(app, record['version'], record['review_token'], 'Reviewed code and original generation references.')
        self.apps.activate(app, record['version'], record['review_token'], 'User reviewed and confirmed exact script rules.')
        return record

    def prepare(self, **parameters):
        return self.apps.prepare('gaussian','lsf',str(self.card),str(self.fixture.root),parameters)['run']

    def test_empty_unknown_and_review_activation_gates(self):
        self.assertEqual(self.apps.list()['applications'], [])
        with self.assertRaises(ApplicationError) as error: self.prepare()
        self.assertEqual(error.exception.code,'application_not_registered')
        record=self.apps.install(str(self.bundle_for()))['application']
        with self.assertRaises(ValueError): self.apps.activate('gaussian',1,record['review_token'],'user confirmed')
        with self.assertRaises(ValueError): self.apps.check('gaussian',1,record['review_token'],'')
        with self.assertRaises(ValueError): self.apps.check('gaussian',1,'bad','reviewed')
        with self.assertRaises(ApplicationError): self.prepare()
        self.assertFalse((self.fixture.root/'submissions').exists())

    def test_static_install_does_not_run_and_exact_mismatch_fails(self):
        bundle=self.bundle_for(); (bundle/'references/0.lsf').write_text('incorrect\n')
        with patch('hpc_mcp.applications.subprocess.Popen') as execute:
            record=self.apps.install(str(bundle))['application'];execute.assert_not_called()
        with self.assertRaises(ApplicationError) as error:
            self.apps.check('gaussian',1,record['review_token'],'code reviewed')
        self.assertEqual(error.exception.code,'application_reference_mismatch')
        self.assertEqual(self.apps.get('gaussian',1)['application']['status'],'draft')
        self.assertFalse(list(self.apps.root.glob('.run-*')))

    def test_qg16_original_direct_and_mcp_snapshot_are_identical(self):
        self.register(); run=self.prepare(exclusive=True,node='node01',exclude_nodes='node02,node03',keep_scratch=True)
        record,path=self.apps.record('gaussian')
        request=run['template']['application_plugin']['request'];params=run['template']['application_plugin']['parameters']
        direct=subprocess.run([sys.executable,'-I','-B',str(path/'handler.py')],input=json.dumps(dict(request,parameters=params,staging_dir=str(self.fixture.root))),text=True,capture_output=True,check=True)
        original=reference('qg16','generate_bsub_content')['generate_bsub_content']
        expected=original('water',SimpleNamespace(queue='Gaussian',nproc=28,exclusive=True,node='node01',exclude_nodes=['node02','node03']),
            Path(run['remote_dir'])/'water.gjf','/share/apps/gaussian/G16B01/AVX2',keep_scratch=True)
        actual=(Path(run['snapshot_dir'])/run['script']).read_bytes()
        self.assertEqual(actual,expected.encode())
        self.assertEqual(actual,json.loads(direct.stdout)['script'].encode())
        self.assertEqual(self.card.read_text(),'# hf/sto-3g\n\nwater\n\n0 1\nH 0 0 0\n\n')
        self.assertFalse(Path(run['remote_dir']).exists())
        self.assertEqual(file_manifest(Path(run['snapshot_dir'])),run['manifest'])

    def test_reviewed_original_cli_preview_files_equal_mcp_generated_files(self):
        import os
        # These two repository CLI preview branches were reviewed above; stub all scheduler calls.
        env=dict(os.environ,PATH=str(self.fixture.bin)+':'+os.environ['PATH'],PYTHONWARNINGS='ignore')
        self.register();run=self.prepare()
        remote=Path(run['remote_dir']);remote.mkdir(parents=True)
        shutil.copy2(self.card,remote/'water.gjf')
        subprocess.run([sys.executable,str(ROOT/'used-scripts/qg16'),'water.gjf','-P','-y'],
            cwd=remote,env=env,text=True,capture_output=True,check=True)
        self.assertEqual((remote/'water.bsub').read_bytes(),(Path(run['snapshot_dir'])/run['script']).read_bytes())
        self.register('vasp')
        for name in ['INCAR','POSCAR','POTCAR','KPOINTS']:(self.fixture.source/name).write_text('fixture\n')
        run=self.apps.prepare('vasp','lsf',str(self.fixture.source),str(self.fixture.root))['run']
        original_dir=self.fixture.root/'original-vasp';original_dir.mkdir()
        subprocess.run([sys.executable,str(ROOT/'used-scripts/qvasp'),'-d','vasp'],cwd=original_dir,
            env=env,text=True,capture_output=True,check=True)
        self.assertEqual((original_dir/'vasp.lsf').read_bytes(),(Path(run['snapshot_dir'])/run['script']).read_bytes())
        self.assertFalse((self.fixture.root/'submissions').exists())

    def test_all_qg16_branches_match_original(self):
        record=self.register(); _,path=self.apps.record('gaussian')
        module=load_handler(path/'handler.py'); original=reference('qg16','generate_bsub_content')['generate_bsub_content']
        versions=module.G16_VERSIONS
        for version,exclusive,node,excluded,local,keep in itertools.product(versions,(False,True),('', 'node01'),('', 'node02,node03'),(False,True),(False,True)):
            params=self.apps.bindings(record['manifest'],dict(version=version,exclusive=exclusive,node=node,exclude_nodes=excluded,local_scratch=local,keep_scratch=keep))
            request={'parameters':params,'input_name':'water.gjf','remote_dir':'/tmp/reference-run'}
            expected=original('water',SimpleNamespace(queue='Gaussian',nproc=28,exclusive=exclusive,node=node or None,exclude_nodes=excluded.split(',') if excluded else None),
                Path('/tmp/reference-run/water.gjf'),versions[version],local_scratch=local,keep_scratch=keep)
            self.assertEqual(module.handle(request)['script'].encode(),expected.encode())

    def test_qvasp_branches_and_mcp_snapshot_match_original(self):
        record=self.register('vasp'); _,path=self.apps.record('vasp');module=load_handler(path/'handler.py')
        original=reference('qvasp','cr_vasp_lsf')
        for name in ['INCAR','POSCAR','POTCAR','KPOINTS']:(self.fixture.source/name).write_text('fixture\n')
        for mpi,version,queue,openmp,optcell,hdf5 in itertools.product(('2015','2018','2019','2020'),('544','620','621'),('xppn2','xp40mc12'),(False,True),(False,True),(False,True)):
            for program in ('std','gam','ncl'):
                params=self.apps.bindings(record['manifest'],dict(mpi_version=mpi,vasp_version=version,queue=queue,openmp=openmp,optcell=optcell,hdf5=hdf5,program_type=program))
                outfile=self.fixture.root/'original.lsf'
                original.update(BIN_DIR=params['bin_dir'],node_cores=28,VASP_LSF=str(outfile))
                original['cr_vasp_lsf'](SimpleNamespace(Jobname='vasp',Queue=queue,Node_Number=4,MPI_version=mpi,VASP_version=version,Program_Type=program,optcell=optcell,openmp=openmp,HDF5=hdf5))
                actual=module.handle({'parameters':params,'staging_dir':str(self.fixture.root)})['script'].encode()
                self.assertEqual(actual,outfile.read_bytes())
        run=self.apps.prepare('vasp','lsf',str(self.fixture.source),str(self.fixture.root),{'hdf5':True})['run']
        result,_=self.apps.execute(record,path,run['template']['application_plugin']['request'])
        self.assertEqual((Path(run['snapshot_dir'])/run['script']).read_bytes(),result['script'].encode())
        self.assertIn('LD_LIBRARY_PATH',result['script'])

    def test_both_scheduler_templates_full_submit_sync_and_remove(self):
        for scheduler in ('lsf','slurm'):
            app='hello_'+scheduler;self.register(app,scheduler)
            (self.fixture.source/'input.txt').write_text('computed\n')
            run=self.apps.prepare(app,scheduler,str(self.fixture.source),str(self.fixture.root))['run']
            self.assertTrue(self.jobs.job_submit(run['run_id'])['ok'])
            self.assertTrue(self.jobs.job_submit(run['run_id'])['already_processed'])
            synced=self.jobs.job_sync(run['run_id'],layout='direct')
            self.assertTrue(synced['ok'],synced)
            self.assertEqual((self.fixture.source/'result.txt').read_text(),'computed\n')
            self.assertFalse((self.fixture.root/'.hpc-mcp-sync'/run['run_id']).exists())
            self.assertTrue(self.apps.remove(app,False)['ok'])
            self.assertEqual(self.jobs.history.get(run['run_id'])['state'],'succeeded')
            (self.fixture.source/'result.txt').unlink()

    def test_version_pinning_restart_and_cluster_change(self):
        first=self.register();run=self.prepare()
        second=self.apps.install(str(self.bundle_for()))['application']
        self.apps.check('gaussian',2,second['review_token'],'reviewed');self.apps.activate('gaussian',2,second['review_token'],'user confirmed')
        restarted=ApplicationService(self.jobs)
        self.assertEqual(restarted.prepare('gaussian','lsf',str(self.card),str(self.fixture.root))['run']['template']['application_plugin']['version'],2)
        self.assertEqual(self.jobs.history.get(run['run_id'])['template']['application_plugin']['version'],1)
        self.jobs.clusters['lsf']=replace(self.jobs.clusters['lsf'],work_root='/tmp/different')
        with self.assertRaises(ApplicationError) as error:self.prepare()
        self.assertEqual(error.exception.code,'application_cluster_changed')

    def test_manifest_rejects_schema_that_cannot_register_a_dynamic_tool(self):
        for field in ('model_config', '_private', 'bad-name'):
            with self.assertRaises(ValueError):schema_check({'type':'object','properties':{field:{'type':'string'}},'additionalProperties':False})
        with self.assertRaises(ValueError):schema_check({'type':'integer','enum':['28']})
        with self.assertRaises(ValueError):schema_check({'type':'string','$ref':'unknown'})

    def test_schema_input_and_installed_file_tamper(self):
        self.register()
        for params in ({'unknown':1},{'cpus':0},{'cpus':True},{'node':'n;touch x'}):
            with self.assertRaises(ValueError):self.prepare(**params)
        with self.assertRaises(ApplicationError):self.apps.prepare('gaussian','slurm',str(self.card),str(self.fixture.root))
        _,path=self.apps.record('gaussian');(path/'handler.py').write_text('print(1)')
        with self.assertRaises(ApplicationError) as error:self.prepare()
        self.assertEqual(error.exception.code,'application_changed')

    def test_removal_blocks_prepared_then_cleans_repeats_and_preserves_data(self):
        self.register();run=self.prepare();preview=self.apps.remove('gaussian')
        self.assertIn(run['run_id'],preview['blockers'])
        with self.assertRaises(ApplicationError):self.apps.remove('gaussian',False)
        self.jobs.job_cancel(run['run_id'])
        result=self.apps.remove('gaussian',False)
        self.assertEqual(result['residual_paths'],[])
        self.assertFalse((self.apps.root/'gaussian').exists())
        self.assertTrue(self.card.exists());self.assertTrue(Path(run['snapshot_dir']).exists())
        self.assertEqual(self.apps.remove('gaussian',False)['status'],'already_removed')
        self.assertEqual(self.apps.list()['applications'],[])

    def test_interrupted_removal_recovers(self):
        self.register()
        with patch('hpc_mcp.applications.shutil.rmtree',side_effect=OSError('interrupted')):
            with self.assertRaises(OSError):self.apps.remove('gaussian',False)
        with self.assertRaises(ApplicationError) as error:self.prepare()
        self.assertEqual(error.exception.code,'application_removing')
        self.assertEqual(self.apps.remove('gaussian',False)['status'],'removed')

    def test_cleanup_only_unreferenced_and_symlink_rejection(self):
        self.register();oldrun=self.prepare();second=self.apps.install(str(self.bundle_for()))['application']
        self.apps.check('gaussian',2,second['review_token'],'reviewed');self.apps.activate('gaussian',2,second['review_token'],'confirmed')
        self.assertEqual(self.apps.cleanup(0)['paths'],[])
        self.jobs.job_cancel(oldrun['run_id'])
        paths=self.apps.cleanup(0,False)['paths'];self.assertEqual(len(paths),1)
        self.assertTrue((self.apps.root/'gaussian/versions/2').exists())
        (self.apps.root/'unsafe').symlink_to(self.fixture.source,target_is_directory=True)
        with self.assertRaises(ValueError):self.apps.cleanup(0,False)
        self.assertTrue(self.card.exists())

    def test_handler_timeout_protocol_and_missing_dependency(self):
        for script,error,extras in [('import time;time.sleep(5)','application_timeout',{'timeout_seconds':1}),
                                    ('print("bad")','application_protocol_error',{}),
                                    ('raise RuntimeError("oops")','application_handler_failed',{}),
                                    ('print("x"*2000)','application_output_limit',{'max_output_bytes':1024})]:
            bundle=self.bundle_for();(bundle/'handler.py').write_text(script)
            manifest=json.loads((bundle/'manifest.json').read_text());manifest.update(extras);(bundle/'manifest.json').write_text(json.dumps(manifest))
            rec=self.apps.install(str(bundle))['application']
            with self.assertRaises(ApplicationError) as actual:self.apps.check('gaussian',rec['version'],rec['review_token'],'reviewed')
            self.assertEqual(actual.exception.code,error)
        self.assertFalse(list(self.apps.root.glob('.run-*')))

    def test_workflow_materialization_rebinds_remote_cwd(self):
        self.register();run=self.prepare()
        directory=self.fixture.root/'derived';shutil.copytree(run['snapshot_dir'],directory)
        (directory/'dependency.chk').write_text('parent\n')
        template=dict(run['template'],workflow_materialization={'workflow_id':'w_test','task_id':run['run_id']})
        result=self.apps.materialize(run,str(directory),template)['run']
        actual=(Path(result['snapshot_dir'])/result['script']).read_text()
        self.assertIn('cd "'+result['remote_dir']+'"',actual)
        self.assertNotIn('cd "'+run['remote_dir']+'"',actual)
        self.assertIn('dependency.chk',{e['path'] for e in result['manifest']})
        self.assertEqual(file_manifest(Path(run['snapshot_dir'])),run['manifest'])

    def test_cli_install_check_activate_prepare_and_unknown_without_config(self):
        command=[sys.executable,'-m','hpc_mcp','--state-dir',str(self.fixture.state),'--config',str(self.fixture.root/'missing.toml')]
        proc=subprocess.run(command+['application-prepare','missing','lsf',str(self.card),'--project-root',str(self.fixture.root)],capture_output=True,text=True)
        self.assertEqual(proc.returncode,1);self.assertEqual(json.loads(proc.stdout)['error'],'application_not_registered')


    def test_registered_marker_validation_success_and_failure_are_separate_from_scheduler(self):
        bundle=self.bundle_for('hello_lsf')
        manifest=json.loads((bundle/'manifest.json').read_text())
        manifest['validation']={'log':'result.txt','success_marker':'Normal termination','failure_marker':'Error termination'}
        (bundle/'manifest.json').write_text(json.dumps(manifest))
        record=self.apps.install(str(bundle))['application']
        self.apps.check('hello_lsf',1,record['review_token'],'reviewed')
        self.apps.activate('hello_lsf',1,record['review_token'],'user confirmed')
        for content,status in [('Normal termination\n','passed'),('Normal termination\nError termination\n','failed')]:
            (self.fixture.source/'input.txt').write_text(content)
            run=self.apps.prepare('hello_lsf','lsf',str(self.fixture.source),str(self.fixture.root))['run']
            self.jobs.job_submit(run['run_id'])
            result=self.apps.validate(run['run_id'])
            self.assertEqual(result['status'],status)
            self.assertEqual(result['scheduler_state'],'succeeded')
            self.assertIn('sha256',result['log'])
            self.assertEqual(self.apps.get('hello_lsf')['application']['validation']['run_id'],run['run_id'])
            (self.fixture.source/'result.txt').unlink(missing_ok=True)

    def test_failed_install_publication_rolls_back_and_missing_dependency_is_explicit(self):
        bundle=self.bundle_for();replace_file=__import__('os').replace
        def fail_publish(source,target):
            if Path(source).name=='bundle':raise OSError('publication interrupted')
            return replace_file(source,target)
        with patch('hpc_mcp.applications.os.replace',side_effect=fail_publish):
            with self.assertRaises(OSError):self.apps.install(str(bundle))
        self.assertEqual(self.apps.list()['applications'],[])
        self.assertFalse(list(self.apps.root.glob('.install-*')))
        manifest=json.loads((bundle/'manifest.json').read_text());manifest['dependencies']=['hpc_nonexistent_dependency_123']
        (bundle/'manifest.json').write_text(json.dumps(manifest));record=self.apps.install(str(bundle))['application']
        with self.assertRaises(ApplicationError) as error:self.apps.check('gaussian',record['version'],record['review_token'],'reviewed')
        self.assertEqual(error.exception.code,'application_dependency_missing')

    def test_default_versions_are_scoped_to_cluster(self):
        from hpc_mcp.config import Cluster
        self.register();self.jobs.clusters['other']=replace(self.jobs.clusters['lsf'],name='other')
        bundle=self.bundle_for();manifest=json.loads((bundle/'manifest.json').read_text())
        manifest['clusters']=['other'];manifest['parameters']['properties']['cpus']['default']=24
        (bundle/'manifest.json').write_text(json.dumps(manifest))
        # Reference fixtures explicitly bind cpus=28, so changed defaults do not change their inputs.
        record=self.apps.install(str(bundle))['application']
        self.apps.check('gaussian',2,record['review_token'],'reviewed');self.apps.activate('gaussian',2,record['review_token'],'user confirmed')
        first=self.prepare();second=self.apps.prepare('gaussian','other',str(self.card),str(self.fixture.root))['run']
        self.assertEqual(first['generation']['spec']['resources']['cpus'],28)
        self.assertEqual(second['generation']['spec']['resources']['cpus'],24)
        self.assertEqual(self.apps.cleanup(0)['paths'],[])

    def test_abandoned_prepared_task_cannot_be_submitted_after_uninstall(self):
        self.register();run=self.prepare()
        abandoned=self.jobs.job_cancel(run['run_id'])
        self.assertTrue(abandoned['local_only'])
        self.apps.remove('gaussian',False)
        with self.assertRaisesRegex(ValueError,'abandoned'):self.jobs.job_submit(run['run_id'])
        self.assertFalse((self.fixture.root/'submissions').exists())


def load_handler(path):
    from types import ModuleType
    module=ModuleType('reviewed_handler'); module.__file__=str(path)
    exec(compile(path.read_text(),str(path),'exec'),module.__dict__)
    return module
