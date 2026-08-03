# Azure AI Foundry Workshop

Hands-on samples and tutorials for Azure AI Foundry, Azure Machine Learning,
agent workflows, evaluation, and batch embeddings.

> Disclaimer: This is a learning/sample artifact — not production hardened.

## Contents

- [Workshop Areas](#workshop-areas)
- [Batch Embeddings Workshop](#batch-embeddings-workshop)
- [Agent Samples](#agent-samples)
- [Useful References](#useful-references)

## Workshop Areas

| Area | Purpose | Status |
|---|---|---|
| `01-foundamentals-v3/` | Foundry agents, workflows, evaluation, and tracing | Available |
| `02-sk-orchestration/` | Semantic Kernel orchestration | Available |
| `03-fdl/` | Foundry development samples | Available |
| `04-logicapp-msgraph-apis/` | Logic Apps and Microsoft Graph integration | Available |
| `05-batch-embedding/` | One-hour AML/APIM batch embedding workshop | Available |

## Batch Embeddings Workshop

The five-page tutorial explains and tests:

1. RPM versus TPM and the solution architecture;
2. pre-provisioned quick start;
3. one-input versus packed-array RPM behavior;
4. direct versus APIM-pooled TPM capacity;
5. AML child metrics, error evidence, and circuit breakers.

Start locally:

```bash
uvx --from mkdocs-material mkdocs serve
```

Then open `http://127.0.0.1:8000/ai-foundry-workshop/`.

## Agent Samples

The `01-foundamentals-*` notebooks showcase Azure AI Foundry capabilities:

* `01-enterpise-knowledge-agent.ipynb` contains sample agent with AI Search, Bing Search tools, block list
* `02-medical-diagnostic-agent.ipynb` contains sample agent with Code Interpeter, File Search tools for data analytics task
* `03-automated-evaluations.ipynb` contains sample of run automated evaluations for the output of a single agent in Foundry
* `04-sequential-multiagent-tracking.ipynb` contains customized OpenTelemetry tracing for a sequential multi-agent workflow.



## Useful References
* Run Azure OpenAI models in Azure Machine Learning batch endpoints: https://learn.microsoft.com/en-us/azure/machine-learning/how-to-use-batch-model-openai-embeddings?view=azureml-api-2&tabs=cli%2Cad
* Components of the Foundry Agent Service: https://blog.langchain.com/context-engineering-for-agents/
* Connected Agents to isolate context: https://learn.microsoft.com/en-us/azure/ai-foundry/agents/how-to/connected-agents?pivots=python
* Context Engineering for Agents: https://blog.langchain.com/context-engineering-for-agents/

## LangChain with Foundry Inference API
* LangChain with Azure Foundry Inference API: https://learn.microsoft.com/en-us/azure/ai-foundry/how-to/develop/langchain
* LangChain with Azure Foundry Tracing: https://learn.microsoft.com/en-us/azure/ai-foundry/how-to/develop/langchain#view-traces
* Plugin Foundry Models in LangChain https://python.langchain.com/docs/integrations/providers/azure_ai/
* LangChain and Microsoft Azure AI Services Integration https://python.langchain.com/docs/integrations/providers/microsoft/

## AutoGen with Foundry Agent Serivce
* AutoGen extension for Foundry Agent Service https://microsoft.github.io/autogen/stable/user-guide/extensions-user-guide/azure-foundry-agent.html#
* AutoGen and Semantic Kernel Integration https://devblogs.microsoft.com/semantic-kernel/semantic-kernel-and-autogen-part-2/

## DeepResearch
* https://learn.microsoft.com/en-us/azure/ai-foundry/agents/how-to/tools/deep-research-samples
* https://learn.microsoft.com/en-us/azure/ai-foundry/agents/how-to/tools/deep-research