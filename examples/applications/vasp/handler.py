import re
import json
import sys
from pathlib import Path
from types import SimpleNamespace

def cr_vasp_lsf(args):
    global BIN_DIR
    line='#BSUB -J ' + args.Jobname + '\n'
    line=line + '#BSUB -q ' + args.Queue + '\n'
    line=line + '#BSUB -R ' + 'span[ptile=' + str(node_cores) + ']\n'
    line=line + '#BSUB -n ' + str(args.Node_Number * node_cores) + '\n'
    line=line + '#BSUB -o ' + args.Jobname + '-%J.out\n'
    if args.MPI_version == '2018':
    # line=line + 'source /share/apps/intel/parallel_studio_xe_2015/psxevars.sh >/dev/null 2>&1 \n'
        line=line + 'source /share/apps/intel_ips_xe_2018u3/parallel_studio_xe_2018/psxevars.sh >/dev/null 2>&1 \n'
    # line=line + 'MPI_HOME=/share/apps/intel/impi/5.0.3.048\n'
        line=line + 'MPI_HOME=/share/apps/intel_ips_xe_2018u3/impi/2018.3.222\n'
    elif args.MPI_version == '2015':
        line=line + 'source /share/apps/intel/parallel_studio_xe_2015/psxevars.sh >/dev/null 2>&1 \n'
        line=line + 'MPI_HOME=/share/apps/intel/impi/5.0.3.048\n'
    elif args.MPI_version == '2019':
        line=line + 'source /share/apps/Intel/intel2019u5/parallel_studio_xe_2019/psxevars.sh >/dev/null 2>&1\n'
        line=line + 'MPI_HOME=/share/apps/Intel/intel2019u5/impi/2019.5.281/intel64/bin\n'

    else:
        #line=line + 'export PATH=/share/apps/scripts:/share/apps/intel/impi/5.0.3.048/bin64 \n'
        #line=line + 'export LD_LIBRARY_PATH=/share/apps/intel/mkl/lib/intel64 \n'
        #line=line + 'MPI_HOME=/share/apps/intel/impi/5.0.3.048\n'
        line=line + 'source /share/apps/intel2020u4/parallel_studio_xe_2020/psxevars.sh  >/dev/null 2>&1\n'
        #line=line + 'MPI_HOME=/share/apps/intel2020u4/intelpython3/bin\n' 
        #line=line + 'module load gcc/7.5.0\n'


    line=line + 'BIN_DIR=' + BIN_DIR + '\n'
    line=line + 'VASP_RUNDIR=`pwd`\n'
    line=line + 'cd $VASP_RUNDIR\n'
    line=line + 'ln -s $BIN_DIR/vdw_kernel.bindat $VASP_RUNDIR/vdw_kernel.bindat\n'
    line=line + 'ln -s $BIN_DIR/vdw_kernel.bindat.big_endian $VASP_RUNDIR/vdw_kernel.big_endian\n'
    line=line + '\n'
    PROG = 'vasp' + args.VASP_version + '_' + args.Program_Type
    if args.Queue not in ['xp40mc12'] :
        PROG += '_AVX512_mkl'
    if args.optcell and args.VASP_version in ['544'] :
        PROG += '_OPTCELL' 
    if args.openmp and args.VASP_version in ['620','621'] :
        PROG += '_openmp'
    if args.HDF5:
        PROG += '.hdf5'
        line=line + 'export LD_LIBRARY_PATH=/apps/Tools/HDF5/hdf5-1.12.2/lib:$LD_LIBRARY_PATH\n'
    #line=line + '$MPI_HOME/mpirun -bootstrap lsf $BIN_DIR/' + PROG + ' >log.out 2>&1 \n'
    line=line + 'mpirun -bootstrap lsf $BIN_DIR/' + PROG + ' >log.out 2>&1 \n'
    
    with open(VASP_LSF,'w') as v_lsf:
       v_lsf.write(line)

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

if __name__ == '__main__':
    print(json.dumps(handle(json.load(sys.stdin))))
