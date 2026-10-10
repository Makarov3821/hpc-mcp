#!/bin/bash
#SBATCH --partition=debug
#SBATCH --ntasks=1
cat input.txt > result.txt
