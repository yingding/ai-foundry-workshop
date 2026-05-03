def extract_agent_data(agent_data, ground_truth="", latency=None):
    """
    :param agent_data: The agent data structure containing messages and responses.
    :param latency: Optional latency value; if not provided, a default will be used.
    :return: A dictionary with extracted information.
    """
    query = ""
    response = ""
    context = ""
    
    # Navigate through the agent data structure to extract relevant information
    if isinstance(agent_data, dict):
        # Handle the specific structure: 'query': [{'role': 'system', 'content': '...'}, {'role': 'user', 'content': [{'type': 'text', 'text': '...'}]}]
        if 'query' in agent_data and isinstance(agent_data['query'], list):
            context_parts = []
            for message in agent_data['query']:
                if isinstance(message, dict):
                    role = message.get('role', '')
                    content = message.get('content', '')
                    
                    if role == 'system':
                        # System message becomes the primary context
                        context_parts.append(content)
                    elif role == 'user':
                        # Extract user query
                        if isinstance(content, list):
                            # Handle nested content structure: [{'type': 'text', 'text': '...'}]
                            for content_item in content:
                                if isinstance(content_item, dict) and content_item.get('type') == 'text':
                                    query = content_item.get('text', '')
                                    break
                        elif isinstance(content, str):
                            # Handle simple string content
                            query = content
            
            # Use system message content directly as context (without "System:" prefix)
            context = " | ".join(context_parts)
        
        # Handle the response structure: 'response': [{'createdAt': '...', 'run_id': '...', 'role': 'assistant', 'content': [{'type': 'text', 'text': '...'}]}]
        if 'response' in agent_data:
            response_data = agent_data['response']
            if isinstance(response_data, list):
                # Find the LAST assistant message in the response array (final response)
                assistant_messages = []
                for message in response_data:
                    if isinstance(message, dict) and message.get('role') == 'assistant':
                        content = message.get('content', [])
                        if isinstance(content, list):
                            # Extract text from nested content structure
                            for content_item in content:
                                if isinstance(content_item, dict) and content_item.get('type') == 'text':
                                    text_content = content_item.get('text', '')
                                    if text_content:  # Only add non-empty text content
                                        assistant_messages.append(text_content)
                                    break
                        elif isinstance(content, str):
                            if content:  # Only add non-empty content
                                assistant_messages.append(content)
                
                # Use the last assistant message as the final response
                if assistant_messages:
                    response = assistant_messages[-1]
            elif isinstance(response_data, str):
                # Handle simple string response
                response = response_data
        
        # Look for query in other possible locations if not found above
        if not query and 'messages' in agent_data:
            for message in agent_data['messages']:
                if message.get('role') == 'user':
                    content = message.get('content', '')
                    if isinstance(content, list):
                        for content_item in content:
                            if isinstance(content_item, dict) and content_item.get('type') == 'text':
                                query = content_item.get('text', '')
                                break
                    else:
                        query = content
                    break
        elif not query and 'conversation' in agent_data:
            # Handle conversation structure
            conversation = agent_data['conversation']
            if isinstance(conversation, list) and len(conversation) > 0:
                first_message = conversation[0]
                if isinstance(first_message, dict):
                    query = first_message.get('content', '') or first_message.get('message', '')
        
        # Look for response in other locations if not found above
        if not response and 'messages' in agent_data:
            for message in agent_data['messages']:
                if message.get('role') == 'assistant':
                    content = message.get('content', '')
                    if isinstance(content, list):
                        # Handle nested content structure
                        for content_item in content:
                            if isinstance(content_item, dict):
                                if content_item.get('type') == 'text':
                                    response = content_item.get('text', '')
                                    break
                    else:
                        response = content
                    break
        elif not response and 'conversation' in agent_data:
            # Get the last assistant message
            conversation = agent_data['conversation']
            if isinstance(conversation, list):
                for message in reversed(conversation):
                    if isinstance(message, dict) and message.get('role') == 'assistant':
                        response = message.get('content', '')
                        break
        
        # Add additional context from other fields (append to existing context)
        additional_context = []
        if 'tools' in agent_data:
            additional_context.append(f"Tools: {agent_data['tools']}")
        
        if 'metadata' in agent_data:
            additional_context.append(f"Metadata: {agent_data['metadata']}")
        
        # Add timestamps and run_id from response if available
        if 'response' in agent_data and isinstance(agent_data['response'], list):
            for message in agent_data['response']:
                if isinstance(message, dict) and message.get('role') == 'assistant':
                    created_at = message.get('createdAt', '')
                    run_id_from_response = message.get('run_id', '')
                    if created_at:
                        additional_context.append(f"Response created: {created_at}")
                    if run_id_from_response:
                        additional_context.append(f"Run ID: {run_id_from_response}")
                    break
        
        # Combine context with additional information
        # if additional_context:
        #     if context:
        #         context = f"{context} | {' | '.join(additional_context)}"
        #     else:
        #         context = " | ".join(additional_context)
    
    # Generate ground truth based on the specific use case
    # ground_truth = ""
    
    # Calculate latency if not provided
    if latency is None:
        latency = 8.5  # Default placeholder, you should measure actual latency
    
    # Calculate response length
    response_length = len(response) if response else 0
    
    return {
        "query": query,
        "ground_truth": ground_truth,
        "response": response,
        "context": context,
        "latency": latency,
        "response_length": response_length
    }