# Introduction
This repository provides sample code of Azure AI Foundry

## Demo Samples
#### `01-Foundamentals` Sub Folder
sample notebooks showcase the Azure AI Foundry capabilities:

* `01-enterpise-knowledge-agent.ipynb` contains sample agent with AI Search, Bing Search tools, block list
* `02-medical-diagnostic-agent.ipynb` contains sample agent with Code Interpeter, File Search tools for data analytics task
* `03-automated-evaluations.ipynb` contains sample of run automated evaluations for the output of a single agent in Foundry
* `04-sequential-multiagent-tracking.ipynb` contains sample of customized opentelemetry tracing of multi agent in a sequential turns tracing with custom function added to opentelemetry trace.



# Useful References
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