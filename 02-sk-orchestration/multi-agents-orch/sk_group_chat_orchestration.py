"""
Group chat orchestration models a collaborative conversation among agents, optionally including a human participant. 
A group chat manager coordinates the flow, determining which agent should respond next and when to request human input. 
This pattern is powerful for simulating meetings, debates, or collaborative problem-solving sessions.

Updated to use Azure AI Agents instead of ChatCompletionAgent.
"""

import asyncio
import os
from dotenv import load_dotenv
from azure.identity.aio import DefaultAzureCredential

from semantic_kernel.agents import Agent, AzureAIAgent, AzureAIAgentSettings
from semantic_kernel.contents import ChatMessageContent
from semantic_kernel.agents import GroupChatOrchestration, RoundRobinGroupChatManager
from semantic_kernel.agents.runtime import InProcessRuntime

# Load environment variables from parent directory
load_dotenv(dotenv_path="../.env")

DEPLOYMENT = os.getenv("MODEL_DEPLOYMENT_NAME")
ENDPOINT = os.getenv("ENDPOINT")
print(f"Using model deployment: {DEPLOYMENT} and endpoint: {ENDPOINT}")

if not DEPLOYMENT or not ENDPOINT:
    raise ValueError("Please set MODEL_DEPLOYMENT_NAME and ENDPOINT environment variables")


def agent_response_callback(message: ChatMessageContent) -> None:
    print(f"**{message.name}**\n{message.content}")

async def main():
    """Run group chat orchestration with Azure AI Agents."""
    
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
        
        # Create Writer Azure AI Agent
        writer_agent_definition = await client.agents.create_agent(
            model=DEPLOYMENT,
            name="Writer",
            description="Content writer that creates and edits content based on feedback",
            instructions="You are an excellent content writer. You create new content and edit contents based on the feedback.",
        )
        
        writer = AzureAIAgent(
            client=client,
            definition=writer_agent_definition,
        )
        
        # Create Reviewer Azure AI Agent
        reviewer_agent_definition = await client.agents.create_agent(
            model=DEPLOYMENT,
            name="Reviewer",
            description="Content reviewer that provides feedback to improve content quality",
            instructions="You are an excellent content reviewer. You review the content and provide feedback to the writer.",
        )
        
        reviewer = AzureAIAgent(
            client=client,
            definition=reviewer_agent_definition,
        )
        
        agents = [writer, reviewer]
        print(f"Created {len(agents)} Azure AI Agents successfully!")
        
        # Run group chat orchestration while client is still active
        group_chat_orchestration = GroupChatOrchestration(
            members=agents,
            manager=RoundRobinGroupChatManager(max_rounds=5),  # Odd number so writer gets the last word
            agent_response_callback=agent_response_callback,
        )

        runtime = InProcessRuntime()
        runtime.start()

        print("Running group chat orchestration...")
        orchestration_result = await group_chat_orchestration.invoke(
            task="Create a slogan for a new electric SUV that is affordable and fun to drive.",
            runtime=runtime,
        )

        value = await orchestration_result.get()
        print(f"\n***** Final Result *****\n{value}")

        await runtime.stop_when_idle()
        print("\nOrchestration completed successfully!")


if __name__ == "__main__":
    asyncio.run(main())