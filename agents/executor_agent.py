import os
from tools.tools_coder_pipeline import (
    ask_human_tool, TOOL_NOT_EXECUTED_WORD, prepare_create_file_tool, prepare_replace_code_tool,
    prepare_insert_code_tool
)
from langchain_openai.chat_models import ChatOpenAI
from typing import TypedDict, Sequence
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langgraph.prebuilt.tool_executor import ToolExecutor, ToolInvocation
from langgraph.graph import StateGraph, END
from dotenv import load_dotenv, find_dotenv
from langchain.tools import tool
from langchain_community.chat_models import ChatOllama
from langchain_anthropic import ChatAnthropic
from langchain_mistralai import ChatMistralAI
from utilities.print_formatters import print_formatted
from utilities.util_functions import (
    check_file_contents, check_application_logs, render_tools, find_tools_json
)
from utilities.langgraph_common_functions import (
    call_model, call_tool, bad_json_format_msg, multiple_jsons_msg, no_json_msg, agent_looped_human_help
)


load_dotenv(find_dotenv())
log_file_path = os.getenv("LOG_FILE")
frontend_port = os.getenv("FRONTEND_PORT")


@tool
def finish(test_instruction):
    """Call that tool when all plan steps are implemented to finish your job.
tool input:
:param test_instruction: write detailed instruction for human what actions he need to do in order to check if
implemented changes work correctly."""
    print_formatted(test_instruction, color="blue")


# llm = ChatTogether(model="meta-llama/Llama-3-70b-chat-hf", temperature=0).with_config({"run_name": "Executor"})
# llm = ChatOllama(model="mixtral"), temperature=0).with_config({"run_name": "Executor"})
llms = []
if os.getenv("ANTHROPIC_API_KEY"):
    llms.append(ChatAnthropic(
        model='claude-3-5-sonnet-20240620', temperature=0, max_tokens=2000, timeout=120
    ).with_config({"run_name": "Executor"}))
if os.getenv("OPENAI_API_KEY"):
    llms.append(ChatOpenAI(model="gpt-4o-mini", temperature=0, timeout=120).with_config({"run_name": "Executor"}))


class AgentState(TypedDict):
    messages: Sequence[BaseMessage]


parent_dir = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))

with open(f"{parent_dir}/prompts/executor_system.prompt", "r") as f:
    system_prompt_template = f.read()


class Executor():
    def __init__(self, files, work_dir):
        self.work_dir = work_dir
        tools = prepare_tools(work_dir)
        rendered_tools = render_tools(tools)
        self.tool_executor = ToolExecutor(tools)
        self.system_message = SystemMessage(
            content=system_prompt_template.format(executor_tools=rendered_tools)
        )
        self.files = files

        # workflow definition
        executor_workflow = StateGraph(AgentState)

        executor_workflow.add_node("agent", self.call_model_executor)
        executor_workflow.add_node("tool", self.call_tool_executor)
        executor_workflow.add_node("human_help", agent_looped_human_help)

        executor_workflow.set_entry_point("agent")

        # executor_workflow.add_edge("agent", "checker")
        executor_workflow.add_edge("tool", "agent")
        executor_workflow.add_edge("human_help", "agent")
        executor_workflow.add_conditional_edges("agent", self.after_agent_condition)

        self.executor = executor_workflow.compile()

    # node functions
    def call_model_executor(self, state):
        state = call_model(state, llms)
        return state

    def call_tool_executor(self, state):
        last_ai_message = state["messages"][-1]
        last_message = state["messages"][-1]
        if not hasattr(last_message, "json5_tool_calls"):
            state["messages"].append(HumanMessage(content="No tool called"))
            return state
        json5_tool_calls = last_message.json5_tool_calls
        # reverse to make code changes from later lines to starting ones
        json5_tool_calls.reverse()
        tool_responses = [self.tool_executor.invoke(ToolInvocation(**tool_call)) for tool_call in json5_tool_calls]
        tool_response = "\n\n###\n\n".join(tool_responses) if len(tool_responses) > 1 else tool_responses[0]
        response_message = HumanMessage(content=tool_response)
        state["messages"].append(response_message)
        for tool_call in last_ai_message.json5_tool_calls:
            if tool_call["tool"] == "create_file_with_code":
                self.files.add(tool_call["tool_input"]["filename"])
        self.exchange_file_contents(state)
        return state

    # Conditional edge functions
    def after_agent_condition(self, state):
        last_message = state["messages"][-1]

        # safety mechanism for looped wrong tool call
        last_human_messages = [m for m in state["messages"] if m.type == "human"][-5:]
        tool_not_executed_msgs = [
            m for m in last_human_messages if isinstance(m.content, str) and m.content.startswith(TOOL_NOT_EXECUTED_WORD)
        ]
        if len(tool_not_executed_msgs) == 4:
            print("Seems like AI been looped. Please suggest it how to introduce change correctly:")
            return "human_help"

        elif last_message.content in (bad_json_format_msg, multiple_jsons_msg, no_json_msg):
            return "agent"
        elif last_message.json5_tool_calls[0]["tool"] == "finish":
            return END
        else:
            return "tool"

    # just functions
    def exchange_file_contents(self, state):
        # Remove old one
        state["messages"] = [msg for msg in state["messages"] if not hasattr(msg, "contains_file_contents")]
        # Add new file contents
        file_contents = check_file_contents(self.files, self.work_dir)
        file_contents_msg = HumanMessage(content=f"File contents:\n{file_contents}", contains_file_contents=True)
        state["messages"].insert(2, file_contents_msg)  # insert after the system and plan msgs
        return state

    def do_task(self, task, plan):
        print("\n\n\nExecutor starting its work")
        file_contents = check_file_contents(self.files, self.work_dir)
        inputs = {"messages": [
            self.system_message,
            HumanMessage(content=f"Task: {task}\n\n######\n\nPlan:\n\n{plan}"),
            HumanMessage(content=f"File contents: {file_contents}", contains_file_contents=True)
        ]}
        final_response = self.executor.invoke(inputs, {"recursion_limit": 150})
        test_instruction = find_tools_json(final_response['messages'][-1].content)[0]["tool_input"]

        return test_instruction, self.files


def prepare_tools(work_dir):
    replace_code = prepare_replace_code_tool(work_dir)
    insert_code = prepare_insert_code_tool(work_dir)
    create_file = prepare_create_file_tool(work_dir)
    tools = [replace_code, insert_code, create_file, ask_human_tool, finish]

    return tools
