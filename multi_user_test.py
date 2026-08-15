import time
import requests
from typing import List
from langchain_ollama import ChatOllama
from langchain_core.tools import tool
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

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


# Initialize Ollama model and bind tools
llm = ChatOllama(model="qwen2.5:14b-instruct")
tools = [save_user_memory, retrieve_user_memory]
llm_with_tools = llm.bind_tools(tools)
tool_map = {tool.name: tool for tool in tools}

def run_agent_turn(messages: list, turn_text: str, user_id: str) -> str:
    messages.append(HumanMessage(content=turn_text))
    response = llm_with_tools.invoke(messages)
    messages.append(response)
    
    if response.tool_calls:
        for tool_call in response.tool_calls:
            tool_name = tool_call["name"]
            # Ensure the model passes the correct user_id
            tool_args = tool_call["args"]
            tool_args["user_id"] = user_id
            
            print(f"  [TRACE] LLM requested tool: {tool_name} with arguments: {tool_args}")
            
            # Execute tool
            tool_func = tool_map[tool_name]
            tool_result = tool_func.invoke(tool_args)
            print(f"  [TRACE] Tool result: {tool_result}")
            
            if tool_name == "save_user_memory":
                time.sleep(3) # Wait for async extraction
                
            messages.append(
                ToolMessage(
                    content=str(tool_result),
                    tool_call_id=tool_call["id"],
                    name=tool_name
                )
            )
        
        final_response = llm_with_tools.invoke(messages)
        messages.append(final_response)
        return final_response.content
    return response.content

def run_user_session(user_id: str, description: str, turns: List[str]):
    print(f"\n==================================================")
    print(f"STARTING SESSION FOR USER: {user_id} ({description})")
    print(f"==================================================")
    
    messages = [
        SystemMessage(
            content=(
                f"You are a helpful assistant. The current user's ID is '{user_id}'. "
                "You have access to tools to save_user_memory and retrieve_user_memory. "
                "Whenever the user shares new facts about themselves, immediately call save_user_memory. "
                "Whenever the user asks a question about their preferences, background, or past info, "
                "immediately call retrieve_user_memory to find the facts before answering. "
                "Answer in English only."
            )
        )
    ]
    
    for i, turn in enumerate(turns, start=1):
        print(f"\n[Turn {i}] User: {turn}")
        assistant_reply = run_agent_turn(messages, turn, user_id)
        print(f"[Turn {i}] Assistant: {assistant_reply}")

# Define 5 users and their 10 turns
user_scenarios = {
    "chef_user": {
        "desc": "Professional Italian Chef",
        "turns": [
            "Hi, my name is Mario and I live in Naples.",
            "I work as a Head Chef specializing in traditional pasta dishes.",
            "In my free time, I enjoy baking sourdough bread.",
            "I absolutely detest using store-bought tomato sauce; fresh Pomodoro is my rule.",
            "My secret ingredient for my signature lasagna is a dash of fresh nutmeg.",
            "Can you tell me what my name is and where I live?",
            "What is my profession and specialization?",
            "What sourdough-related hobby do I have, and what is my lasagna secret ingredient?",
            "Do I play the cello or hike on weekends?",
            "Is my favorite game Elden Ring?"
        ]
    },
    "musician_user": {
        "desc": "Classical Violinist & Composer",
        "turns": [
            "Hello, my name is Clara and I live in Vienna.",
            "I am a professional orchestra musician playing the cello.",
            "On weekends, I love writing chamber music compositions.",
            "I really dislike loud drum sets; they ruin the acoustic balance.",
            "My secret dream is to perform a solo at the Royal Albert Hall.",
            "Can you tell me what my name is and where I live?",
            "What instrument do I play and what is my job?",
            "What do I write on weekends, and what is my secret performing dream?",
            "Do I use store-bought tomato sauce or cook lasagna?",
            "What distance runner am I?"
        ]
    },
    "doctor_user": {
        "desc": "Pediatrician & Outdoor Enthusiast",
        "turns": [
            "Hello, my name is Sarah and I live in Boston.",
            "I work as a Pediatrician at the city hospital.",
            "I love hiking in the mountains during my holidays.",
            "My favorite drink after a long shift is hot chamomile tea.",
            "My secret relaxation method is listening to bird sounds in the forest.",
            "Can you tell me what my name is and where I live?",
            "What is my occupation and where do I work?",
            "How do I spend my holidays and what is my favorite post-shift drink?",
            "Do I play the cello or perform at the Royal Albert Hall?",
            "Do I bake sourdough bread?"
        ]
    },
    "gamer_user": {
        "desc": "Hardcore Console Gamer",
        "turns": [
            "Hey, my name is Alex and I live in Tokyo.",
            "I work as a Game QA Tester for indie studio releases.",
            "On weekends, I stream RPG game playthroughs.",
            "I prefer using a PS5 controller over a mouse and keyboard.",
            "My secret achievement is beating Elden Ring without taking any damage.",
            "Can you tell me what my name is and where I live?",
            "What is my job and where do I work?",
            "What do I do on weekends and what is my preferred gaming controller?",
            "Do I listen to bird sounds in the forest to relax?",
            "What lasagna secret ingredient do I use?"
        ]
    },
    "runner_user": {
        "desc": "Marathon Runner",
        "turns": [
            "Hi, my name is John and I live in Denver.",
            "I am a professional athletics coach specializing in long-distance runners.",
            "Every morning, I go for a 10km trail run.",
            "My preferred running shoe brand is Brooks.",
            "My secret target is to run the Boston Marathon under 2 hours and 30 minutes.",
            "Can you tell me what my name is and where I live?",
            "What is my profession and what do I do every morning?",
            "What brand of running shoes do I wear and what is my marathon goal?",
            "Do I work as a Game QA Tester or stream RPGs?",
            "Do I hate store-bought tomato sauce?"
        ]
    },
    "astronomer_user": {
        "desc": "Astronomer",
        "turns": [
            "Hello, my name is Leo and I live in Hawaii.",
            "I work as an Astronomer at the Mauna Kea Observatory.",
            "My favorite hobby is stargazing using my personal telescope.",
            "I prefer drinking hot cocoa while observing the night sky.",
            "My secret target is to discover a new habitable exoplanet.",
            "Can you tell me what my name is and where I live?",
            "What is my profession and where do I work?",
            "What do I prefer to drink and what is my secret target?",
            "Do I work as a Pediatrician or play the cello?",
            "Do I wear Brooks running shoes?"
        ]
    }
}

if __name__ == "__main__":
    print("Starting Multi-User Separation & Integrity Test...")
    for user_id, details in user_scenarios.items():
        run_user_session(user_id, details["desc"], details["turns"])
    print("\nMulti-User Separation & Integrity Test Completed.")
