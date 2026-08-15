import requests
from typing import List, Optional
from langchain_core.tools import tool

MEMORY_API_BASE = "http://localhost:8000"

@tool
def save_user_memory(user_id: str, text: str) -> str:
    """
    Asynchronously saves a new fact or context about a user to long-term memory.
    Use this whenever the user shares personal preferences, updates their status, 
    or provides factual background information worth remembering.
    """
    try:
        response = requests.post(
            f"{MEMORY_API_BASE}/ingest",
            json={"user_id": user_id, "text": text},
            timeout=5.0
        )
        if response.status_code == 202:
            return f"Successfully queued memory ingestion for user {user_id}."
        return f"Failed to save memory. Status: {response.status_code}"
    except Exception as e:
        return f"Error saving memory: {str(e)}"


@tool
def retrieve_user_memory(user_id: str, query: str, top_k: int = 3) -> str:
    """
    Retrieves relevant past facts and context about a specific user based on a search query.
    Use this before answering personal questions or when relevant background state is needed.
    """
    try:
        response = requests.post(
            f"{MEMORY_API_BASE}/retrieve",
            json={
                "user_id": user_id,
                "query": query,
                "top_k": top_k,
                "half_life_days": 30.0
            },
            timeout=5.0
        )
        if response.status_code == 200:
            memories: List[str] = response.json().get("memories", [])
            if not memories:
                return "No relevant past memories found."
            formatted = "\n".join([f"- {m}" for m in memories])
            return f"Retrieved Memories:\n{formatted}"
        return f"Failed to retrieve memory. Status: {response.status_code}"
    except Exception as e:
        return f"Error retrieving memory: {str(e)}"


# Agent Initialization Example
from langchain_ollama import ChatOllama
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

llm = ChatOllama(model="qwen2.5:14b-instruct")
tools = [save_user_memory, retrieve_user_memory]
llm_with_tools = llm.bind_tools(tools)

if __name__ == "__main__":
    import json
    
    USER_ID = "langchain_test_user_v2"
    print(f"=== Starting 5-Turn Agent Memory Test for User: {USER_ID} ===\n")
    
    # Map tool name to tool object
    tool_map = {tool.name: tool for tool in tools}
    
    # Conversation history with System Message
    messages = [
        SystemMessage(
            content=(
                f"You are a helpful assistant. The current user's ID is '{USER_ID}'. "
                "You have access to tools to save_user_memory and retrieve_user_memory. "
                "Whenever the user shares new facts about themselves, immediately call save_user_memory. "
                "Whenever the user asks a question about their preferences, background, or past info, "
                "immediately call retrieve_user_memory to find the facts before answering."
            )
        )
    ]
    
    # 5 test turns
    turns = [
        "Hi, my name is Vikas and I live in Bangalore.",
        "I work as a Software Engineer specializing in Generative AI.",
        "I also enjoy playing tennis on weekends.",
        "Can you tell me where I live and what my profession is?",
        "What do I like to do on weekends, and what is my focus area in engineering?"
    ]
    
    for i, turn in enumerate(turns, start=1):
        print(f"\n--- TURN {i} ---")
        print(f"[USER]: {turn}")
        
        # Add user prompt to history
        messages.append(HumanMessage(content=turn))
        
        # First LLM call
        response = llm_with_tools.invoke(messages)
        messages.append(response)
        
        # Print LLM raw response details
        if response.content:
            print(f"[LLM Response]: {response.content}")
        
        # Process tool calls if any
        if response.tool_calls:
            for tool_call in response.tool_calls:
                tool_name = tool_call["name"]
                tool_args = tool_call["args"]
                print(f"[TRACE] LLM requested tool: {tool_name} with arguments: {tool_args}")
                
                # Execute the tool
                tool_func = tool_map[tool_name]
                tool_result = tool_func.invoke(tool_args)
                print(f"[TRACE] Tool result: {tool_result}")
                
                if tool_name == "save_user_memory":
                    import time
                    print("[TRACE] Sleeping 3s for asynchronous ingestion to complete...")
                    time.sleep(3)
                
                # Append tool response message
                messages.append(
                    ToolMessage(
                        content=str(tool_result),
                        tool_call_id=tool_call["id"],
                        name=tool_name
                    )
                )
            
            # Call LLM again so it can consume the tool output
            final_response = llm_with_tools.invoke(messages)
            messages.append(final_response)
            print(f"[LLM Final Response]: {final_response.content}")
        else:
            if not response.content:
                print("[LLM Response]: (Empty response / No tool call made)")