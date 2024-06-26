#!/bin/bash

#SBATCH --partition=gpu
#SBATCH --gpus=1
#SBATCH --job-name=DL2
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=1
#SBATCH --time=00:00:05
#SBATCH --output=scripts/slurm_logs/slurm_output_%A.out

cd $HOME/development/dl2
source .venv/bin/activate
srun echo $1
deactivate