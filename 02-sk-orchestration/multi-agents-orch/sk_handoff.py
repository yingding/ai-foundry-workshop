"""
Handoff orchestration allows agents to transfer control to one another based on the context or user request. 
Each agent can “handoff” the conversation to another agent with the appropriate expertise, ensuring that the right agent handles each part of the task.
This is particularly useful in customer support, expert systems, or any scenario requiring dynamic delegation.
"""
import asyncio
import os
from dotenv import load_dotenv
from azure.identity.aio import DefaultAzureCredential
from semantic_kernel.functions import kernel_function
from semantic_kernel.agents import AzureAIAgent, AzureAIAgentSettings
from semantic_kernel.agents import OrchestrationHandoffs
from semantic_kernel.contents import ChatMessageContent
from semantic_kernel.contents import AuthorRole, ChatMessageContent, FunctionCallContent, FunctionResultContent
from semantic_kernel.agents import HandoffOrchestration
from semantic_kernel.agents.runtime import InProcessRuntime
from semantic_kernel.agents import Agent, HandoffOrchestration, OrchestrationHandoffs

# Load environment variables
load_dotenv(dotenv_path="../.env")

DEPLOYMENT = os.getenv("MODEL_DEPLOYMENT_NAME")
ENDPOINT = os.getenv("ENDPOINT")
print(f"Using model deployment: {DEPLOYMENT} and endpoint: {ENDPOINT}")

if not DEPLOYMENT or not ENDPOINT:
    raise ValueError("Please set MODEL_DEPLOYMENT_NAME and ENDPOINT environment variables")

# These plugins will contain the logic for handling specific tasks.
class OrderStatusPlugin:
    @kernel_function
    def check_order_status(self, order_id: str) -> str:
        """Check the status of an order."""
        # Simulate checking the order status
        return f"Order {order_id} is shipped and will arrive in 2-3 days."


class OrderRefundPlugin:
    @kernel_function
    def process_refund(self, order_id: str, reason: str) -> str:
        """Process a refund for an order."""
        # Simulate processing a refund
        print(f"Processing refund for order {order_id} due to: {reason}")
        return f"Refund for order {order_id} has been processed successfully."


class OrderReturnPlugin:
    @kernel_function
    def process_return(self, order_id: str, reason: str) -> str:
        """Process a return for an order."""
        # Simulate processing a return
        print(f"Processing return for order {order_id} due to: {reason}")
        return f"Return for order {order_id} has been processed successfully."

# Define the agents that will use these plugins.
async def get_agents(client) -> tuple[list[Agent], OrchestrationHandoffs]:
    """Return a list of agents that will participate in the Handoff orchestration and the handoff relationships.

    Feel free to add or remove agents and handoff connections.
    """
    print("Creating Azure AI Agents...")
    
    # Create Support Agent
    support_agent_definition = await client.agents.create_agent(
        model=DEPLOYMENT,
        name="TriageAgent",
        description="A customer support agent that triages issues.",
        instructions="Handle customer requests and determine which specialist agent should handle their issue.",
    )
    
    support_agent = AzureAIAgent(
        client=client,
        definition=support_agent_definition,
    )

    # Create Refund Agent
    refund_agent_definition = await client.agents.create_agent(
        model=DEPLOYMENT,
        name="RefundAgent", 
        description="A customer support agent that handles refunds.",
        instructions="Handle refund requests. Use the process_refund function when processing refunds.",
        # Note: Azure AI Agents handle tools differently - you may need to configure tools separately
    )
    
    refund_agent = AzureAIAgent(
        client=client,
        definition=refund_agent_definition,
    )

    # Create Order Status Agent
    order_status_agent_definition = await client.agents.create_agent(
        model=DEPLOYMENT,
        name="OrderStatusAgent",
        description="A customer support agent that checks order status.",
        instructions="Handle order status requests. Use the check_order_status function to check order status.",
    )
    
    order_status_agent = AzureAIAgent(
        client=client,
        definition=order_status_agent_definition,
    )

    # Create Order Return Agent  
    order_return_agent_definition = await client.agents.create_agent(
        model=DEPLOYMENT,
        name="OrderReturnAgent",
        description="A customer support agent that handles order returns.",
        instructions="Handle order return requests. Use the process_return function when processing returns.",
    )
    
    order_return_agent = AzureAIAgent(
        client=client,
        definition=order_return_agent_definition,
    )

    # Define the handoff orchestration logic.

    handoffs = (
        OrchestrationHandoffs()
        .add_many(    # Use add_many to add multiple handoffs to the same source agent at once
            source_agent=support_agent.name,
            target_agents={
                refund_agent.name: "Transfer to this agent if the issue is refund related",
                order_status_agent.name: "Transfer to this agent if the issue is order status related",
                order_return_agent.name: "Transfer to this agent if the issue is order return related",
            },
        )
        .add(    # Use add to add a single handoff
            source_agent=refund_agent.name,
            target_agent=support_agent.name,
            description="Transfer to this agent if the issue is not refund related",
        )
        .add(
            source_agent=order_status_agent.name,
            target_agent=support_agent.name,
            description="Transfer to this agent if the issue is not order status related",
        )
        .add(
            source_agent=order_return_agent.name,
            target_agent=support_agent.name,
            description="Transfer to this agent if the issue is not order return related",
        )
    )
    return [support_agent, refund_agent, order_status_agent, order_return_agent], handoffs


def agent_response_callback(message: ChatMessageContent) -> None:
    """Observer function to print the messages from the agents.

    Please note that this function is called whenever the agent generates a response,
    including the internal processing messages (such as tool calls) that are not visible
    to other agents in the orchestration.
    """
    print(f"{message.name}: {message.content}")
    for item in message.items:
        if isinstance(item, FunctionCallContent):
            print(f"Calling '{item.name}' with arguments '{item.arguments}'")
        if isinstance(item, FunctionResultContent):
            print(f"Result from '{item.name}' is '{item.result}'")

# A key feature of handoff orchestration is the ability for a human to participate in the conversation.

def human_response_function() -> ChatMessageContent:
    user_input = input("User: ")
    return ChatMessageContent(role=AuthorRole.USER, content=user_input)

async def main():
    """Run handoff orchestration with Azure AI Agents."""
    
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
        # Get the agents and handoffs
        agents, handoffs = await get_agents(client)
        support_agent, refund_agent, order_status_agent, order_return_agent = agents
        
        print(f"Created {len(agents)} Azure AI Agents successfully!")
        
        handoff_orchestration = HandoffOrchestration(
            members=agents,
            handoffs=handoffs,
            agent_response_callback=agent_response_callback,
            human_response_function=human_response_function,
        )

        runtime = InProcessRuntime()
        runtime.start()

        print("Running handoff orchestration...")
        orchestration_result = await handoff_orchestration.invoke(
            task="A customer is on the line.",
            runtime=runtime,
        )

        value = await orchestration_result.get()
        print(f"\n***** Final Result *****\n{value}")

        await runtime.stop_when_idle()
        print("\nOrchestration completed successfully!")

if __name__ == "__main__":
    asyncio.run(main())