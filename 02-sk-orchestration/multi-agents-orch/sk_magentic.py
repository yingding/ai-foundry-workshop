"""
It is a flexible, general-purpose multi-agent pattern designed for complex, open-ended tasks that require dynamic collaboration. 
In this pattern, a dedicated Magentic manager coordinates a team of specialized agents, selecting which agent should act next based on the evolving context, task progress, and agent capabilities.
The Magentic manager maintains a shared context, tracks progress, and adapts the workflow in real time. 
This enables the system to break down complex problems, delegate subtasks, and iteratively refine solutions through agent collaboration. 
The orchestration is especially well-suited for scenarios where the solution path is not known in advance and may require multiple rounds of reasoning, research, and computation.
"""
import asyncio
import os
from dotenv import load_dotenv
from azure.identity.aio import DefaultAzureCredential
from azure.identity import DefaultAzureCredential as SyncDefaultAzureCredential

from semantic_kernel.agents import (
    Agent,
    AzureAIAgent,
    AzureAIAgentSettings,
    MagenticOrchestration,
    StandardMagenticManager,
)
from semantic_kernel.connectors.ai.open_ai import AzureChatCompletion

from semantic_kernel.contents import ChatMessageContent
from semantic_kernel.agents.runtime import InProcessRuntime

# Load environment variables from parent directory
load_dotenv(dotenv_path="../.env")

DEPLOYMENT = os.getenv("MODEL_DEPLOYMENT_NAME")
ENDPOINT = os.getenv("ENDPOINT")
print(f"Using model deployment: {DEPLOYMENT} and endpoint: {ENDPOINT}")

if not DEPLOYMENT or not ENDPOINT:
    raise ValueError("Please set MODEL_DEPLOYMENT_NAME and ENDPOINT environment variables")

async def agents(client) -> list[Agent]:
    """Return a list of agents that will participate in the Magentic orchestration.

    Feel free to add or remove agents.
    """
    print("Creating Azure AI Agents...")
    
    # Create Research Agent
    research_agent_definition = await client.agents.create_agent(
        model=DEPLOYMENT,
        name="ResearchAgent",
        description="A helpful assistant that finds information and performs research.",
        instructions=(
            "You are a Researcher. You find information without additional computation or quantitative analysis. "
            "Focus on gathering accurate data and facts from reliable sources."
        ),
    )
    
    research_agent = AzureAIAgent(
        client=client,
        definition=research_agent_definition,
    )

    # Create Analysis Agent (replacing the code interpreter agent)
    analysis_agent_definition = await client.agents.create_agent(
        model=DEPLOYMENT,
        name="AnalysisAgent",
        description="A helpful assistant that performs data analysis and computations.",
        instructions=(
            "You are an Analyst. You solve questions using mathematical analysis and logical reasoning. "
            "Provide detailed analysis, calculations, and computation processes. "
            "Create tables and structured output when presenting data comparisons."
        ),
    )
    
    analysis_agent = AzureAIAgent(
        client=client,
        definition=analysis_agent_definition,
    )

    return [research_agent, analysis_agent]


def agent_response_callback(message: ChatMessageContent) -> None:
    """Observer function to print the messages from the agents."""
    print(f"**{message.name}**\n{message.content}")


async def main():
    """Run Magentic orchestration with Azure AI Agents."""
    
    # Get Azure AI Agent settings
    ai_agent_settings = AzureAIAgentSettings(
        model_deployment_name=DEPLOYMENT,
        endpoint=ENDPOINT
    )
    
    # Keep the client connection alive for the entire orchestration
    async with (
        DefaultAzureCredential() as creds,
        AzureAIAgent.create_client(credential=creds, endpoint=ENDPOINT) as client,
    ):
        # Create Azure chat completion service for the manager with proper authentication
        sync_creds = SyncDefaultAzureCredential()
        azure_service = AzureChatCompletion(
            deployment_name=DEPLOYMENT,
            endpoint="https://fasazureaihub5942341532.openai.azure.com/",
           
        )
        
        # Get the agents
        agent_list = await agents(client)
        print(f"Created {len(agent_list)} Azure AI Agents successfully!")
        
        # Create a Magentic orchestration with Azure AI agents and a Magentic manager
        # Note, the Standard Magentic manager uses prompts that have been tuned very
        # carefully but it accepts custom prompts for advanced users and scenarios.
        # For even more advanced scenarios, you can subclass the MagenticManagerBase
        # and implement your own manager logic.
        # The standard manager also requires a chat completion model that supports
        # structured output.
        magentic_orchestration = MagenticOrchestration(
            members=agent_list,
            manager=StandardMagenticManager(chat_completion_service=azure_service),
            agent_response_callback=agent_response_callback,
        )

        # Create a runtime and start it
        runtime = InProcessRuntime()
        runtime.start()

        print("Running Magentic orchestration...")
        # Invoke the orchestration with a task and the runtime
        orchestration_result = await magentic_orchestration.invoke(
            task=(
                "I am preparing a report on the energy efficiency of different machine learning model architectures. "
                "Compare the estimated training and inference energy consumption of ResNet-50, BERT-base, and GPT-2 "
                "on standard datasets (e.g., ImageNet for ResNet, GLUE for BERT, WebText for GPT-2). "
                "Then, estimate the CO2 emissions associated with each, assuming training on an Azure Standard_NC6s_v3 VM "
                "for 24 hours. Provide tables for clarity, and recommend the most energy-efficient model "
                "per task type (image classification, text classification, and text generation)."
            ),
            runtime=runtime,
        )

        # Wait for the results
        value = await orchestration_result.get()

        print(f"\n***** Final Result *****\n{value}")

        # Stop the runtime when idle
        await runtime.stop_when_idle()
        print("\nOrchestration completed successfully!")


if __name__ == "__main__":
    asyncio.run(main())