from langchain_ollama import ChatOllama
from langchain_core.messages import HumanMessage, SystemMessage

llm = ChatOllama(
    model="qwen2.5-coder:7b",
    temperature=0.3,
    num_ctx=16384,
)

system_prompt = r"""You are an expert Python trading bot engineer.
Project root: C:\Users\Owner\PythonTrading\stock-bot

You ALWAYS look at the actual files in the project.
When I ask to fix something, read the relevant file (run_all.py, config.py, pipeline_strategies.py, etc.) and give the EXACT code change needed.
Do not give example code from scratch. Fix the real code."""


def ask(prompt):
    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=prompt)
    ]
    response = llm.invoke(messages)
    print(response.content)

if __name__ == "__main__":
    print("Ollama Chat Ready! Type your request or 'exit' to quit.")
    while True:
        user_input = input("\nYou: ")
        if user_input.lower() in ["exit", "quit"]:
            break
        ask(user_input)
