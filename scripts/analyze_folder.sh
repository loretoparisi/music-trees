# first argument should be the parent folder for all runs to be run
# second argument should be the CUDA device to use
# third argument should be the output name of this analysis
for dir in "${1}"/*
do
  echo ${dir}
  export CUDA_VISIBLE_DEVICES=${2}$ && python3.7 music_trees/analyze.py ${dir} ${3}$
done