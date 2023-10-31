#!/bin/bash
# THIS FILE IS GENERATED BY AUTOMATION SCRIPT! PLEASE REFER TO ORIGINAL SCRIPT!
# THIS FILE IS MODIFIED AUTOMATICALLY FROM TEMPLATE AND SHOULD BE RUNNABLE!

#SBATCH --partition=gp4d
#SBATCH --account=MST111300

### This script works for any number of nodes, Ray will find and manage all resources
#SBATCH --gres=gpu:8
#SBATCH --nodes=2
#SBATCH --ntasks-per-node=1
#SBATCH --gpus-per-task=8
#SBATCH --cpus-per-task=32
#SBATCH --nodelist=gn0818,gn0914
### Give all resources to a single Ray task, ray can manage the resources internally

# Load modules or your own conda environment here

module purge
module load miniconda3
module load cuda/11.7
conda activate LibMultiLabel

# ===== DO NOT CHANGE THINGS HERE UNLESS YOU KNOW WHAT YOU ARE DOING =====
# This script is a modification to the implementation suggest by gregSchwartz18 here:
# https://github.com/ray-project/ray/issues/826#issuecomment-522116599
redis_password=$(uuidgen)
export redis_password

nodes=$(scontrol show hostnames "$SLURM_JOB_NODELIST") # Getting the node names
nodes_array=($nodes)

node_1=${nodes_array[0]}
ip=$(srun --nodes=1 --ntasks=1 -w "$node_1" hostname --ip-address) # making redis-address

# if we detect a space character in the head node IP, we'll
# convert it to an ipv4 address. This step is optional.
if [[ "$ip" == *" "* ]]; then
  IFS=' ' read -ra ADDR <<< "$ip"
  if [[ ${#ADDR[0]} -gt 16 ]]; then
    ip=${ADDR[1]}
  else
    ip=${ADDR[0]}
  fi
  echo "IPV6 address detected. We split the IPV4 address as $ip"
fi

port=6379
ip_head=$ip:$port
export ip_head
echo "IP Head: $ip_head"

echo "STARTING HEAD at $node_1"
srun --nodes=1 --ntasks=1 -w "$node_1" --gres=gpu:8\
  ray start --head --node-ip-address="$ip" --port=$port --redis-password="$redis_password" --block --num-cpus "${SLURM_CPUS_PER_TASK}" --num-gpus "${SLURM_GPUS_PER_TASK}"&
sleep 30

worker_num=$((SLURM_JOB_NUM_NODES - 1)) #number of nodes other than the head node
for ((i = 1; i <= worker_num; i++)); do
  node_i=${nodes_array[$i]}
  echo "STARTING WORKER $i at $node_i"
  srun --nodes=1 --ntasks=1 -w "$node_i" --gres=gpu:8 ray start --address "$ip_head" --redis-password="$redis_password" --block --num-cpus "${SLURM_CPUS_PER_TASK}" --num-gpus "${SLURM_GPUS_PER_TASK}"&
  sleep 5
done

# ===== Call your code below =====
TUNE_MAX_PENDING_TRIALS_PG=100 python -u search_params_parallel.py --config /work/d11922012/gordon/mlgroup/example_config/EUR-Lex/tune/cnn_tanhW_mlp_tune.yml --no_retrain
