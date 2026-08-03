# Microsoft Foundry Tutorials

Step-by-step workshops for Microsoft Foundry and Azure Machine Learning.

> Disclaimer: These are learning/sample artifacts — not production hardened.

## 📚 Contents

- [Microsoft Foundry Tutorials](#microsoft-foundry-tutorials)
  - [📚 Contents](#contents)
  - [📝 Overview](#overview)
  - [📦 Tutorials](#tutorials)
    - [1. Batch Embeddings on Microsoft Foundry and Azure Machine Learning](#1-batch-embeddings-on-microsoft-foundry-and-azure-machine-learning)
  - [📂 Repository Structure](#repository-structure)

## 📝 Overview

| Tutorial | Focus | Azure services | Status |
|---|---|---|---|
| 1. Batch Embeddings | Packing, RPM/TPM evidence, pooled capacity | Microsoft Foundry, Azure Machine Learning, API Management | **Available** |

## 📦 Tutorials

### 1. [Batch Embeddings on Microsoft Foundry and Azure Machine Learning](05-batch-embedding/index.md)

Build and evaluate an asynchronous document-embedding workflow using the ADA
Embedding Model, Azure Machine Learning batch endpoints, request packing,
MLflow metrics, and an Azure API Management backend pool.

| Aspect | Detail |
|---|---|
| Duration | 1 hour |
| Model | `text-embedding-ada-002` (ADA Embedding Model) |
| Batch orchestration | Azure Machine Learning pipeline component deployment |
| Capacity routing | Azure API Management backend pool |
| Evidence | AML child-job MLflow metrics and structured output artifacts |

**What you will learn**

- distinguish client request amplification from model-side RPM and TPM limits;
- pack Azure embedding input arrays while preserving output correlation;
- derive experiment targets from deployment-specific capacity;
- compare direct and APIM-pooled routes with controlled evidence;
- classify HTTP 429 feedback without guessing the active limiter;
- inspect and export AML child-job metrics.

➡️ [Start the batch embeddings tutorial](05-batch-embedding/index.md)

---

## 📂 Repository Structure

```text
01-foundamentals-v3/  ← Foundry agent and workflow samples
02-sk-orchestration/  ← Semantic Kernel orchestration samples
03-fdl/               ← Foundry development samples
04-logicapp-msgraph-apis/
05-batch-embedding/   ← Tutorial 1: batch embeddings
```
