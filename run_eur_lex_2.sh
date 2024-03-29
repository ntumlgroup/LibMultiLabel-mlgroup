set -e
for seed in 172;do
    for model in bilstm_tanhW_mlp_tune bilstm_tanhW_tune bilstm_tanh_mlp_tune bilstm_tanh_tune bilstm_vanilla_mlp_tune bilstm_vanilla_tune cnn_tanhW_mlp_tune cnn_tanhW_tune cnn_tanh_mlp_tune cnn_tanh_tune cnn_vanilla_mlp_tune cnn_vanilla_tune;do
        python search_params.py --config example_config/EUR-Lex/tune/$model.yml --no_retrain --no_checkpoint --seed $seed
    done
done
