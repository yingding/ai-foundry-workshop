"""
The concurrent pattern enables multiple agents to work on the same task in parallel.
Each agent processes the input independently, and their results are collected and aggregated.
This approach is well-suited for scenarios where diverse perspectives or solutions are valuable, such as brainstorming, ensemble reasoning, or voting systems.

Updated to use Azure AI Agents instead of ChatCompletionAgent.
"""

import asyncio
import os
from dotenv import load_dotenv
from azure.identity.aio import DefaultAzureCredential

from semantic_kernel.agents import Agent, AzureAIAgent, AzureAIAgentSettings
from semantic_kernel.agents import ConcurrentOrchestration
from semantic_kernel.agents.runtime import InProcessRuntime

# Load environment variables
load_dotenv(dotenv_path="../.env")

DEPLOYMENT  = os.getenv("MODEL_DEPLOYMENT_NAME")
ENDPOINT = os.getenv("ENDPOINT")
print(f"Using model deployment: {DEPLOYMENT} and endpoint: {ENDPOINT}")

if not DEPLOYMENT or not ENDPOINT:
    raise ValueError("Please set MODEL_DEPLOYMENT_NAME and ENDPOINT environment variables")


async def main():
    """Run concurrent orchestration with Azure AI Agents."""
    
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
        print("Creating Azure AI Agents...")
        
        # Create Physics Expert Azure AI Agent
        physics_agent_definition = await client.agents.create_agent(
            model=DEPLOYMENT,
            name="PhysicsExpert",
            instructions="You are an expert in physics. You answer questions from a physics perspective with detailed explanations and examples.",
        )
        
        physics_agent = AzureAIAgent(
            client=client,
            definition=physics_agent_definition,
        )
        
        # Create Chemistry Expert Azure AI Agent
        chemistry_agent_definition = await client.agents.create_agent(
            model=DEPLOYMENT,
            name="ChemistryExpert",
            instructions="You are an expert in chemistry. You answer questions from a chemistry perspective with detailed explanations and examples.",
        )
        
        chemistry_agent = AzureAIAgent(
            client=client,
            definition=chemistry_agent_definition,
        )
        
        agents = [physics_agent, chemistry_agent]
        print(f"Created {len(agents)} Azure AI Agents successfully!")
        
        # Run orchestration while client is still active
        concurrent_orchestration = ConcurrentOrchestration(members=agents)

        runtime = InProcessRuntime()
        runtime.start()

        print("Running concurrent orchestration...")
        orchestration_result = await concurrent_orchestration.invoke(
            task="What is temperature?",
            runtime=runtime,
        )
        
        value = await orchestration_result.get(timeout=30)
        
        print("\n" + "="*50)
        print("RESULTS FROM AZURE AI AGENTS")
        print("="*50)
        
        # For the concurrent orchestration, the result is a list of chat messages
        for item in value:
            print(f"\n# {item.name}:")
            print(f"{item.content}")
            print("-" * 40)
        
        await runtime.stop_when_idle()
        print("\nOrchestration completed successfully!")

if __name__ == "__main__":
    asyncio.run(main())