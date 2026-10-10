# Source after the existing locked prefix activation; keep weights/env/cache on NVMe.
export POSTTRAIN_DATASET_DIR=/nas/chenyi/posttraining-qwen/datasets
export POSTTRAIN_CHECKPOINT_DIR=/nas/chenyi/posttraining-qwen/checkpoints
export POSTTRAIN_RUNTIME_ROOT=/nas/chenyi/posttraining-qwen/runtime
export POSTTRAIN_LOG_DIR=/nas/chenyi/posttraining-qwen/logs
export POSTTRAIN_TRAJECTORY_DIR=$POSTTRAIN_RUNTIME_ROOT/trajectories
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
export DEMO_DISABLE_LLM=1
