# Create
```shell
# pushd /Users/yingding/Code/VCS/ai/llm-train;
VERSION=3.12;
ENV_NAME="azfdydemo${VERSION}";
source ./envtools/create_env.sh -p ~/Code/VENV -e ${ENV_NAME} -v $VERSION;
# popd;
```

# Activate
```shell
VERSION=3.12;
ENV_NAME="azfdydemo${VERSION}";
PROJ_PATH="$HOME/Code/VCS/agents/ai-foundry-workshop"
FOLDER_PATH="01-foundamentals-v2"
source ~/Code/VENV/${ENV_NAME}/bin/activate;
# cd ${PROJ_PATH};
python3 -m pip install -r ${PROJ_PATH}/${FOLDER_PATH}/requirements_fdy.txt --no-cache;
```