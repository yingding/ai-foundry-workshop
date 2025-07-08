"""
In the sequential pattern, agents are organized in a pipeline. 
Each agent processes the task in turn, passing its output to the next agent in the sequence. 
This is ideal for workflows where each step builds upon the previous one, such as document review, data processing pipelines, or multi-stage reasoning.
"""
import asyncio
import os
from dotenv import load_dotenv
from semantic_kernel.agents import Agent, ChatCompletionAgent
from semantic_kernel.connectors.ai.open_ai import AzureChatCompletion
from semantic_kernel.agents.runtime import InProcessRuntime
from semantic_kernel.contents import ChatMessageContent
from semantic_kernel.agents import SequentialOrchestration



# Load environment variables
load_dotenv()


def get_agents() -> list[Agent]:
    azure_service = AzureChatCompletion(
        deployment_name=os.getenv("MODEL_DEPLOYMENT_NAME"),
        endpoint=os.getenv("OPENAI_ENDPOINT"),
        api_version="2024-02-01"  # Use appropriate API version
    )
    concept_extractor_agent = ChatCompletionAgent(
        name="ConceptExtractorAgent",
        instructions=(
            "You are a marketing analyst. Given a product description, identify:\n"
            "- Key features\n"
            "- Target audience\n"
            "- Unique selling points\n\n"
        ),
        service=azure_service,
    )
    writer_agent = ChatCompletionAgent(
        name="WriterAgent",
        instructions=(
            "You are a marketing copywriter. Given a block of text describing features, audience, and USPs, "
            "compose a compelling marketing copy (like a newsletter section) that highlights these points. "
            "Output should be short (around 150 words), output just the copy as a single text block."
        ),
        service=azure_service,
    )
    format_proof_agent = ChatCompletionAgent(
        name="FormatProofAgent",
        instructions=(
            "You are an editor. Given the draft copy, correct grammar, improve clarity, ensure consistent tone, "
            "give format and make it polished. Output the final improved copy as a single text block."
        ),
        service=azure_service,
    )
    return [concept_extractor_agent, writer_agent, format_proof_agent]

def agent_response_callback(message: ChatMessageContent) -> None:
    print(f"# {message.name}\n{message.content}")


async def main():
    agents = get_agents()
    sequential_orchestration = SequentialOrchestration(
        members=agents,
        agent_response_callback=agent_response_callback,
    )
    
    runtime = InProcessRuntime()
    runtime.start()
    orchestration_result = await sequential_orchestration.invoke(
    task="An eco-friendly stainless steel water bottle that keeps drinks cold for 24 hours",
    runtime=runtime,
    )

    value = await orchestration_result.get(timeout=20)
    print(f"***** Final Result *****\n{value}")

    await runtime.stop_when_idle()

if __name__ == "__main__":
    asyncio.run(main())