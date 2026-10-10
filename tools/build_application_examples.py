"""Build reviewed migration examples from repository sources, never run their submission CLI."""
import ast
import json
from pathlib import Path
import warnings

ROOT = Path(__file__).resolve().parents[1]


def source_function(text, name):
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', SyntaxWarning)
        tree = ast.parse(text)
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    return ast.get_source_segment(text, node)


def schema(properties):
    return {'type': 'object', 'properties': properties, 'additionalProperties': False}


def prop(kind, default, **extra):
    return {'type': kind, 'default': default, **extra}


def build():
    for app, source in [('gaussian', 'qg16'), ('vasp', 'qvasp')]:
        text = (ROOT / 'examples' / 'applications' / app / 'original' / source).read_text()
        bundle = ROOT / 'examples' / 'applications' / app
        (bundle / 'original').mkdir(parents=True, exist_ok=True)
        (bundle / 'original' / source).write_text(text)
        header = 'import re\nimport json\nimport sys\nfrom pathlib import Path\nfrom types import SimpleNamespace\n\n'
        if app == 'gaussian':
            with warnings.catch_warnings():
                warnings.simplefilter('ignore', SyntaxWarning)
                tree = ast.parse(text)
            constants = {n.targets[0].id: ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                         and isinstance(n.targets[0], ast.Name) and n.targets[0].id == 'G16_VERSIONS'}
            header += 'G16_VERSIONS = ' + repr(constants['G16_VERSIONS']) + '\n\n'
            function = source_function(text, 'generate_bsub_content')
            wrapper = '''
def handle(request):
    p = request['parameters']
    name = request['input_name']
    if not name or Path(name).suffix != '.gjf':
        raise ValueError('qg16 expects one .gjf card, normalize it before preparing')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', name) or not re.fullmatch(r'/[A-Za-z0-9_./ -]+', request['remote_dir']):
        raise ValueError('name/path cannot be represented in original qg16 quoting')
    if p['local_scratch'] and ' ' in request['remote_dir']:
        raise ValueError('original local scratch export cannot represent spaces')
    stem = Path(name).stem
    args = SimpleNamespace(queue=p['queue'], nproc=p['cpus'], exclusive=p['exclusive'],
        node=p['node'] or None, exclude_nodes=p['exclude_nodes'].split(',') if p['exclude_nodes'] else None)
    script = generate_bsub_content(stem, args, Path(request['remote_dir']) / name,
        G16_VERSIONS[p['version']], local_scratch=p['local_scratch'], keep_scratch=p['keep_scratch'])
    return {'script': script, 'input_files': [name] + p['dependencies'],
        'outputs': [stem + '.log', stem + '.chk'], 'resources': {'queue': p['queue'], 'cpus': p['cpus']}}
'''
            props = {'queue': prop('string', 'Gaussian', pattern='[A-Za-z0-9_.-]+'),
                'cpus': prop('integer', 28, minimum=1), 'version': prop('string', 'AVX2', enum=list(constants['G16_VERSIONS'])),
                'exclusive': prop('boolean', False), 'node': prop('string', '', pattern='[A-Za-z0-9_.-]*'),
                'exclude_nodes': prop('string', '', pattern='(?:[A-Za-z0-9_.-]+(?:,[A-Za-z0-9_.-]+)*)?'),
                'local_scratch': prop('boolean', False), 'keep_scratch': prop('boolean', False),
                'dependencies': prop('array', [], items={'type': 'string'}, maxItems=64)}
            validation = {'log': '{input_stem}.log', 'success_marker': 'Normal termination of Gaussian', 'failure_marker': 'Error termination'}
        else:
            function = source_function(text, 'cr_vasp_lsf')
            wrapper = '''
def handle(request):
    global BIN_DIR, node_cores, VASP_LSF
    p = request['parameters']
    BIN_DIR = p['bin_dir']
    node_cores = p['cores_per_node']
    VASP_LSF = str(Path(request['staging_dir']) / 'vasp.lsf')
    args = SimpleNamespace(Jobname=p['job_name'], Queue=p['queue'], Node_Number=p['nodes'],
        MPI_version=p['mpi_version'], VASP_version=p['vasp_version'], Program_Type=p['program_type'],
        optcell=p['optcell'], openmp=p['openmp'], HDF5=p['hdf5'])
    cr_vasp_lsf(args)
    return {'script': Path(VASP_LSF).read_text(), 'input_files': p['input_files'],
        'outputs': p['outputs'], 'resources': {'queue': p['queue'], 'cpus': p['nodes'] * node_cores,
        'nodes': p['nodes'], 'cpus_per_node': node_cores}}
'''
            props = {'queue': prop('string', 'xppn2', pattern='[A-Za-z0-9_.-]+'), 'job_name': prop('string','vasp',pattern='[A-Za-z0-9_.-]+'),
                'nodes': prop('integer',4,minimum=1), 'cores_per_node': prop('integer',28,minimum=1,maximum=192),
                'mpi_version': prop('string','2020',pattern='[A-Za-z0-9_.-]+'), 'vasp_version': prop('string','621',pattern='[0-9]+'),
                'program_type': prop('string','std',enum=['std','gam','ncl']), 'optcell': prop('boolean',False),
                'openmp': prop('boolean',False), 'hdf5': prop('boolean',False),
                'bin_dir': prop('string','/apps/vasp/bin',pattern='/[A-Za-z0-9_./-]+'),
                'input_files': prop('array',['INCAR','POSCAR','POTCAR','KPOINTS'],items={'type':'string'},maxItems=128),
                'outputs': prop('array',['log.out','OUTCAR','CONTCAR','CHGCAR','WAVECAR'],items={'type':'string'},maxItems=128)}
            validation = None
        code = header + function + '\n' + wrapper + "\nif __name__ == '__main__':\n    print(json.dumps(handle(json.load(sys.stdin))))\n"
        (bundle / 'handler.py').write_text(code)
        manifest = {'interface_version':1,'application':app,'scheduler':'lsf','clusters':['lab'],
            'description':'Reviewed migration example, not auto-installed. Adapt site paths and cluster before use.',
            'parameters':schema(props),'dependencies':[]}
        if validation:manifest['validation']=validation
        (bundle / 'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    for scheduler in ('lsf','slurm'):
        bundle=ROOT/'examples'/'applications'/('hello_'+scheduler)
        (bundle/'original').mkdir(parents=True,exist_ok=True)
        text = '#!/bin/bash\n' + ('#BSUB -q Single\n#BSUB -n 1\n' if scheduler=='lsf' else '#SBATCH --partition=debug\n#SBATCH --ntasks=1\n') + 'cat input.txt > result.txt\n'
        (bundle/'original'/'job.sh').write_text(text)
        (bundle/'handler.py').write_text('import json,sys\nfrom pathlib import Path\nrequest=json.load(sys.stdin)\nprint(json.dumps({"script": (Path(__file__).parent/"original"/"job.sh").read_text(), "input_files":["input.txt"],"outputs":["result.txt"]}))\n')
        (bundle/'manifest.json').write_text(json.dumps({'interface_version':1,'application':'hello_'+scheduler,'scheduler':scheduler,'clusters':['lab'],'parameters':schema({})},indent=2)+'\n')

if __name__=='__main__':build()
