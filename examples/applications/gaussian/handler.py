import re
import json
import sys
from pathlib import Path
from types import SimpleNamespace

G16_VERSIONS = {'AVX2': '/share/apps/gaussian/G16B01/AVX2', 'AVX': '/share/apps/gaussian/G16B01/AVX', 'SSE4': '/share/apps/gaussian/G16B01/SSE4', 'Legacy': '/share/apps/gaussian/G16B01/Legacy', 'Linda': '/share/apps/gaussian/G16B01/Linda', 'AMD': '/share/apps/gaussian/G16B01/AMDGu2021', 'A03': '/share/apps/gaussian/G16A03', 'C02': '/share/home/ylzhao/software/g16_c02'}

def generate_bsub_content(job_name, args, input_file, g16_root_path, mem_limit_mb=None,
                          local_scratch=False, keep_scratch=False):
    """生成原本的单作业 LSF 脚本 (支持内存限制)"""
    g16_exec = f"{g16_root_path}/g16/g16"
    content = [
        f"#BSUB -J {job_name}",
        f"#BSUB -q {args.queue}",
        f"#BSUB -n {args.nproc}",
        f"#BSUB -R span[ptile={args.nproc}]",
    ]
    
    # [新增] 内存资源控制
    if mem_limit_mb and mem_limit_mb > 0:
        # select: 确保节点有足够空闲内存
        # rusage: 告诉调度器预留这些内存，防止过载
        #content.append(f'#BSUB -R "select[mem>{mem_limit_mb}MB] rusage[mem={mem_limit_mb}MB]"')
        #content.append(f'#BSUB -R "rusage[mem={mem_limit_mb}M]"')
        # -M: 硬限制，超过此内存会被 kill (根据需求是否开启，通常建议开启以防溢出)
        #content.append(f"#BSUB -M {mem_limit_mb}M")
        pass

    content.append(f"#BSUB -o {job_name}-%J.out")
    
    if args.exclusive:
        content.append("#BSUB -x")
    if args.node:
        content.append(f"#BSUB -m {args.node}")
    # 如果指定了要排除的节点
    if getattr(args, 'exclude_nodes', None):
        joined = ' && '.join([f"hname!='{n}'" for n in args.exclude_nodes])
        content.append(f'#BSUB -R "select[{joined}]"')

    content.append("")
    content.append(f"export g16root={g16_root_path}")
    content.append(f". {g16_root_path}/g16/bsd/g16.profile")

    # 本地 scratch: 在 gjf 文件所在目录创建 GAUSS_SCRDIR (包含 $LSB_JOBID)
    input_path_obj = Path(input_file).resolve()
    if local_scratch:
        try:
            parent_dir = str(input_path_obj.parent)
        except Exception:
            parent_dir = "$PWD"
        content.append(f"export GAUSS_SCRDIR={parent_dir}/g16_scr_$LSB_JOBID")
    else:
        content.append("export GAUSS_SCRDIR=/scratch/scr/$USER/$LSB_JOBID")

    content.append("mkdir -p $GAUSS_SCRDIR")
    content.append("")

    parent_dir = str(input_path_obj.parent)
    file_name = input_path_obj.name
    
    content.append(f"ORIG_DIR=\"$PWD\"")
    content.append(f"cd \"{parent_dir}\"")

    content.append("echo 'Job started at' `date`")
    content.append(f"echo 'Processing: {file_name}'")
    content.append(f"{g16_exec} < \"{file_name}\" > \"{input_path_obj.stem}.log\"")
    content.append("")
    
    # [修改] 核心：清理与保留逻辑
    if keep_scratch:
        # 获取输入文件的 stem (不带扩展名的文件名)
        # 注意：这里我们在 Shell 脚本中动态获取，或者直接用 Python 传进去的 input_path_obj.stem
        # 既然 gjf 就在当前目录运行，目标目录就是 $PWD (或者 absolute path)
        target_stem = input_path_obj.stem
        target_dir = str(input_path_obj.parent)
        
        content.append("# --- Keep Scratch Mode ---")
        content.append(f"echo 'Moving and renaming scratch files to {target_dir} ...'")
        # 遍历 Scratch 目录下的所有文件
        # 逻辑：获取扩展名 -> mv 原文件 目标目录/stem.扩展名
        content.append(f'for f in "$GAUSS_SCRDIR"/*; do')
        content.append(f'    if [ -f "$f" ]; then')
        content.append(f'        ext="${{f##*.}}"') # Shell 参数扩展获取后缀
        content.append(f'        mv -f "$f" "{target_dir}/{target_stem}.${{ext}}"')
        content.append(f'    fi')
        content.append(f'done')
        # 移完之后删除空目录
        content.append("rm -rf $GAUSS_SCRDIR")
    else:
        # 默认直接删除
        content.append("rm -rf $GAUSS_SCRDIR")
    content.append("echo 'Job finished at' `date`")
    content.append(f"cd \"$ORIG_DIR\"")
    return "\n".join(content)

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

if __name__ == '__main__':
    print(json.dumps(handle(json.load(sys.stdin))))
