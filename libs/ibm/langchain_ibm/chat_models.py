import json
import logging
import os
from datetime import datetime
from operator import itemgetter
from typing import (
    Any,
    Callable,
    Dict,
    Iterator,
    List,
    Literal,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Type,
    TypedDict,
    Union,
    cast,
)
import re
import uuid

from ibm_watsonx_ai import Credentials  # type: ignore
from ibm_watsonx_ai.foundation_models import ModelInference  # type: ignore
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import LanguageModelInput
from langchain_core.language_models.chat_models import (
    BaseChatModel,
    LangSmithParams,
    generate_from_stream,
)
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    BaseMessageChunk,
    ChatMessage,
    ChatMessageChunk,
    FunctionMessage,
    FunctionMessageChunk,
    HumanMessage,
    HumanMessageChunk,
    InvalidToolCall,
    SystemMessage,
    SystemMessageChunk,
    ToolCall,
    ToolMessage,
    ToolMessageChunk,
    convert_to_messages,
)
from langchain_core.output_parsers import JsonOutputParser, PydanticOutputParser
from langchain_core.output_parsers.base import OutputParserLike
from langchain_core.output_parsers.openai_tools import (
    JsonOutputKeyToolsParser,
    PydanticToolsParser,
)
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.prompt_values import ChatPromptValue
from langchain_core.pydantic_v1 import BaseModel, Field, SecretStr, root_validator
from langchain_core.runnables import Runnable, RunnableMap, RunnablePassthrough
from langchain_core.tools import BaseTool
from langchain_core.utils import convert_to_secret_str, get_from_dict_or_env
from langchain_core.utils.function_calling import (
    convert_to_openai_function,
    convert_to_openai_tool,
)

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    ChatMessage,
    FunctionMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)


logger = logging.getLogger(__name__)

RESPONSE_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "description": "Schema for a message with conditional role based on the presence of tools",
    "properties": {
        "role": {
            "type": "string",
            "description": "The role of the message, which should be 'assistant' if there are no tools, or 'tool' if there are tools.",
            "enum": ["assistant", "tool"],
        },
        "content": {
            "type": "string",
            "description": "The content of the message.",
        },
    },
    "required": ["role", "content"],
    "allOf": [
        {
            "if": {"properties": {"tools": {"type": "array", "minItems": 1}}},
            "then": {"properties": {"role": {"const": "tool"}}},
            "else": {"properties": {"role": {"const": "assistant"}}},
        }
    ],
}

TOOL_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "The name of the tool."},
        "description": {
            "type": "string",
            "description": "A description of what the tool does.",
        },
        "args": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "object",
                    "properties": {
                        "description": {
                            "type": "string",
                            "description": "The description of the query argument.",
                        }
                    },
                    "required": ["description"],
                }
            },
            "required": ["query"],
        },
    },
    "required": ["name", "description", "args"],
}


def _is_json(input: str) -> bool:
    try:
        json.loads(input)
        return json.loads(input)
    except json.JSONDecodeError:
        return False


def remove_tags_and_parse(input_string):
    """Removes the specified tags from the input string and parses the result as JSON."""
    tags_to_remove = "<|python_tag|><|start_header_id|>assistant<|end_header_id|>"
    cleaned_string = input_string.replace(tags_to_remove, "").strip()

    if isinstance(cleaned_string, str):
        if _is_json(cleaned_string):
            return _convert_to_json(cleaned_string)
    return cleaned_string.replace("```", "")


def _convert_to_json(input: str) -> dict:
    try:
        return json.loads(input)
    except json.JSONDecodeError:
        raise ValueError("Input is not a valid JSON string")


def _is_valid_tool_call_format(tool: dict) -> bool:
    # If tool is a string, parse it as JSON
   
   
    if isinstance(tool, str):
        try:
            tool = json.loads(tool)
        except json.JSONDecodeError:
            return False

    # Check if tool is a list and extract the dictionary if it is
    if isinstance(tool, list):
       
        if len(tool) == 1 and isinstance(tool[0], dict):
            tool = tool[0]
        else:
           
            return False

    # Check if tool is a dictionary
    if not isinstance(tool, dict):
       
        return False

    # Check if "args" is a dictionary
    if not isinstance(tool.get("args"), dict):
       
        return False

    # Optionally, you can add more checks for specific keys in "args"
    if "query" not in tool["args"]:
        return False

    return True


def _tool_calling(
    raw_tool_calls: dict,
    call_id: str,
) -> BaseMessage:
    """Convert a dictionary to a LangChain message.

    Args:
        tool_call: The dictionary.
        call_id: call id
        tool_call_chunks: tool call chunks

    Returns:
        The LangChain message.
    """
    content = ""
    tool_calls = []


    if isinstance(raw_tool_calls, str):
        raw_tool_calls = json.loads(raw_tool_calls)
    # Check if raw_tool_calls is a list and extract the dictionary if it is
    if isinstance(raw_tool_calls, list):
        if len(raw_tool_calls) == 1 and isinstance(raw_tool_calls[0], dict):
            raw_tool_calls = raw_tool_calls[0]
        else:
            raise ValueError("Expected a list with a single dictionary element")
    elif isinstance(raw_tool_calls, dict):
        raw_tool_calls = [raw_tool_calls]

    for tool in raw_tool_calls:
        tool.update({"id": call_id})
        tool_calls.append(tool)

    return AIMessage(content=content, tool_calls=tool_calls)



## Process a message after it has been generated by the model
def _post_processing(_dict: Mapping[str, Any], call_id: str) -> BaseMessage:
    """Convert a dictionary to a LangChain message.

    Args:
        _dict: The dictionary.
        call_id: call id

    Returns:
        The LangChain message.
    """
    pattern = r'[A-Za-z0-9\s,.-]+'
    raw_message = remove_tags_and_parse(_dict.get("generated_text", ""))

    if _is_valid_tool_call_format(raw_message):
        return _tool_calling(raw_message, call_id)
    elif isinstance(raw_message, dict):
        return AIMessage(content=raw_message.get("content", ""), tool_calls=raw_message.get("tool_calls", []))
    else:
       
        matches = re.findall(pattern, raw_message.replace("\n", ""))
        return AIMessage(content=''.join(matches))




def _format_message_content(content: Any) -> Any:
    """Format message content."""
    if content and isinstance(content, list):
        # Remove unexpected block types
        formatted_content = []
        for block in content:
            if (
                isinstance(block, dict)
                and "type" in block
                and block["type"] == "tool_use"
            ):
                continue
            else:
                formatted_content.append(block)
    else:
        formatted_content = content

    return formatted_content


# TODO: REMOVE
def _lc_tool_call_to_openai_tool_call(tool_call: ToolCall) -> dict:
    return {
        "type": "function",
        "id": tool_call["id"],
        "function": {
            "name": tool_call["name"],
            "arguments": json.dumps(tool_call["args"]),
        },
    }


# TODO: REMOVE
def _lc_invalid_tool_call_to_openai_tool_call(
    invalid_tool_call: InvalidToolCall,
) -> dict:
    return {
        "type": "function",
        "id": invalid_tool_call["id"],
        "function": {
            "name": invalid_tool_call["name"],
            "arguments": invalid_tool_call["args"],
        },
    }


# TODO: REMOVE
def _convert_dict_to_message(messages: List[Dict[str, Any]]) -> List[BaseMessage]:
    """Convert a list of dictionaries to LangChain messages.

    Args:
        messages: The list of dictionaries.

    Returns:
        The list of LangChain messages.
    """
    
   
    langchain_messages = []
    for message in messages:
       
        json_message = json.loads(message)
        if json_message.get("role") == "system":
            langchain_messages.append(SystemMessage(content=json_message["content"]))
        elif json_message.get("role") == "assistant":
            langchain_messages.append(AIMessage(content=json_message["content"], tool_calls=json_message.get("tool_calls", [])))
        elif json_message.get("role") == "user":
            langchain_messages.append(HumanMessage(content=json_message["content"]))
        elif json_message.get("role") == "tool":
            langchain_messages.append(ToolMessage(content=json_message["content"]))
    return langchain_messages


def _convert_message_to_dict(message: BaseMessage) -> dict:
    """Convert a LangChain message to a dictionary.

    Args:
        message: The LangChain message.

    Returns:
        The dictionary.
    """

    if isinstance(message, ChatMessage):
        message_dict = {"role": message.role, "content": message.content}
    elif isinstance(message, HumanMessage):
        message_dict = {"role": "user", "content": message.content}
    elif isinstance(message, AIMessage):
        message_dict = {"role": "assistant", "content": message.content}
        if "function_call" in message.additional_kwargs:
            message_dict["function_call"] = message.additional_kwargs["function_call"]
            # If function call only, content is None not empty string
            if message_dict["content"] == "":
                message_dict["content"] = None
        if "tool_calls" in message.additional_kwargs:
            message_dict["tool_calls"] = message.additional_kwargs["tool_calls"]
            # If tool calls only, content is None not empty string
            if message_dict["content"] == "":
                message_dict["content"] = None
        if "message.tool_calls":
            message_dict["tool_calls"] = message.tool_calls
           
    elif isinstance(message, SystemMessage):
        message_dict = {"role": "system", "content": message.content}
    elif isinstance(message, FunctionMessage):
        message_dict = {
            "role": "function",
            "content": message.content,
            "name": message.name,
        }
    elif isinstance(message, ToolMessage):
        message_dict = {
            "role": "tool",
            "content": message.content,
            "tool_call_id": message.tool_call_id,
        }
    else:
        raise TypeError(f"Got unknown type {message}")
    if "name" in message.additional_kwargs:
        message_dict["name"] = message.additional_kwargs["name"]

    return message_dict


def _convert_delta_to_message_chunk(
    _dict: Mapping[str, Any], default_class: Type[BaseMessageChunk]
) -> BaseMessageChunk:
    id_ = "sample_id"
    role = cast(str, _dict.get("role"))
    content = cast(str, _dict.get("generated_text") or "")
    additional_kwargs: Dict = {}
    if _dict.get("function_call"):
        function_call = dict(_dict["function_call"])
        if "name" in function_call and function_call["name"] is None:
            function_call["name"] = ""
        additional_kwargs["function_call"] = function_call
    tool_call_chunks = []
    if raw_tool_calls := _dict.get("tool_calls"):
        additional_kwargs["tool_calls"] = raw_tool_calls
        try:
            tool_call_chunks = [
                {
                    "name": rtc["function"].get("name"),
                    "args": rtc["function"].get("arguments"),
                    "id": rtc.get("id"),
                    "index": rtc["index"],
                }
                for rtc in raw_tool_calls
            ]
        except KeyError:
            pass

    if role == "user" or default_class == HumanMessageChunk:
        return HumanMessageChunk(content=content, id=id_)
    elif role == "assistant" or default_class == AIMessageChunk:
        return AIMessageChunk(
            content=content,
            additional_kwargs=additional_kwargs,
            id=id_,
            tool_call_chunks=tool_call_chunks,  # type: ignore[arg-type]
        )
    elif role == "system" or default_class == SystemMessageChunk:
        return SystemMessageChunk(content=content, id=id_)
    elif role == "function" or default_class == FunctionMessageChunk:
        return FunctionMessageChunk(content=content, name=_dict["name"], id=id_)
    elif role == "tool" or default_class == ToolMessageChunk:
        return ToolMessageChunk(
            content=content, tool_call_id=_dict["tool_call_id"], id=id_
        )
    elif role or default_class == ChatMessageChunk:
        return ChatMessageChunk(content=content, role=role, id=id_)
    else:
        return default_class(content=content, id=id_)  # type: ignore


class _FunctionCall(TypedDict):
    name: str


class ChatWatsonx(BaseChatModel):
    """
    IBM watsonx.ai large language chat models.

    To use, you should have ``langchain_ibm`` python package installed,
    and the environment variable ``WATSONX_APIKEY`` set with your API key, or pass
    it as a named parameter to the constructor.


    Example:
        .. code-block:: python

            from ibm_watsonx_ai.metanames import GenTextParamsMetaNames
            parameters = {
                GenTextParamsMetaNames.DECODING_METHOD: "sample",
                GenTextParamsMetaNames.MAX_NEW_TOKENS: 100,
                GenTextParamsMetaNames.MIN_NEW_TOKENS: 1,
                GenTextParamsMetaNames.TEMPERATURE: 0.5,
                GenTextParamsMetaNames.TOP_K: 50,
                GenTextParamsMetaNames.TOP_P: 1,
            }

            from langchain_ibm import ChatWatsonx
            watsonx_llm = ChatWatsonx(
                model_id="meta-llama/llama-3-70b-instruct",
                url="https://us-south.ml.cloud.ibm.com",
                apikey="*****",
                project_id="*****",
                params=parameters,
            )
    """

    model_id: str = ""
    """Type of model to use."""

    deployment_id: str = ""
    """Type of deployed model to use."""

    project_id: str = ""
    """ID of the Watson Studio project."""

    space_id: str = ""
    """ID of the Watson Studio space."""

    url: Optional[SecretStr] = None
    """Url to Watson Machine Learning or CPD instance"""

    apikey: Optional[SecretStr] = None
    """Apikey to Watson Machine Learning or CPD instance"""

    token: Optional[SecretStr] = None
    """Token to CPD instance"""

    password: Optional[SecretStr] = None
    """Password to CPD instance"""

    username: Optional[SecretStr] = None
    """Username to CPD instance"""

    instance_id: Optional[SecretStr] = None
    """Instance_id of CPD instance"""

    version: Optional[SecretStr] = None
    """Version of CPD instance"""

    params: Optional[dict] = None
    """Chat Model parameters to use during generate requests."""

    verify: Union[str, bool] = ""
    """User can pass as verify one of following:
        the path to a CA_BUNDLE file
        the path of directory with certificates of trusted CAs
        True - default path to truststore will be taken
        False - no verification will be made"""

    streaming: bool = False
    """ Whether to stream the results or not. """

    watsonx_model: ModelInference = Field(default=None, exclude=True)  #: :meta private:

    class Config:
        """Configuration for this pydantic object."""

        allow_population_by_field_name = True

    @classmethod
    def is_lc_serializable(cls) -> bool:
        return False

    @property
    def _llm_type(self) -> str:
        """Return type of chat model."""
        return "watsonx-chat"

    def _get_ls_params(
        self, stop: Optional[List[str]] = None, **kwargs: Any
    ) -> LangSmithParams:
        """Get standard params for tracing."""
        params = super()._get_ls_params(stop=stop, **kwargs)
        params["ls_provider"] = "together"
        params["ls_model_name"] = self.model_id
        return params

    @property
    def lc_secrets(self) -> Dict[str, str]:
        """A map of constructor argument names to secret ids.

        For example:
            {
                "url": "WATSONX_URL",
                "apikey": "WATSONX_APIKEY",
                "token": "WATSONX_TOKEN",
                "password": "WATSONX_PASSWORD",
                "username": "WATSONX_USERNAME",
                "instance_id": "WATSONX_INSTANCE_ID",
            }
        """
        return {
            "url": "WATSONX_URL",
            "apikey": "WATSONX_APIKEY",
            "token": "WATSONX_TOKEN",
            "password": "WATSONX_PASSWORD",
            "username": "WATSONX_USERNAME",
            "instance_id": "WATSONX_INSTANCE_ID",
        }

    @root_validator()
    def validate_environment(cls, values: Dict) -> Dict:
        """Validate that credentials and python package exists in environment."""
        values["url"] = convert_to_secret_str(
            get_from_dict_or_env(values, "url", "WATSONX_URL")
        )
        if "cloud.ibm.com" in values.get("url", "").get_secret_value():
            values["apikey"] = convert_to_secret_str(
                get_from_dict_or_env(values, "apikey", "WATSONX_APIKEY")
            )
        else:
            if (
                not values["token"]
                and "WATSONX_TOKEN" not in os.environ
                and not values["password"]
                and "WATSONX_PASSWORD" not in os.environ
                and not values["apikey"]
                and "WATSONX_APIKEY" not in os.environ
            ):
                raise ValueError(
                    "Did not find 'token', 'password' or 'apikey',"
                    " please add an environment variable"
                    " `WATSONX_TOKEN`, 'WATSONX_PASSWORD' or 'WATSONX_APIKEY' "
                    "which contains it,"
                    " or pass 'token', 'password' or 'apikey'"
                    " as a named parameter."
                )
            elif values["token"] or "WATSONX_TOKEN" in os.environ:
                values["token"] = convert_to_secret_str(
                    get_from_dict_or_env(values, "token", "WATSONX_TOKEN")
                )
            elif values["password"] or "WATSONX_PASSWORD" in os.environ:
                values["password"] = convert_to_secret_str(
                    get_from_dict_or_env(values, "password", "WATSONX_PASSWORD")
                )
                values["username"] = convert_to_secret_str(
                    get_from_dict_or_env(values, "username", "WATSONX_USERNAME")
                )
            elif values["apikey"] or "WATSONX_APIKEY" in os.environ:
                values["apikey"] = convert_to_secret_str(
                    get_from_dict_or_env(values, "apikey", "WATSONX_APIKEY")
                )
                values["username"] = convert_to_secret_str(
                    get_from_dict_or_env(values, "username", "WATSONX_USERNAME")
                )
            if not values["instance_id"] or "WATSONX_INSTANCE_ID" not in os.environ:
                values["instance_id"] = convert_to_secret_str(
                    get_from_dict_or_env(values, "instance_id", "WATSONX_INSTANCE_ID")
                )
        credentials = Credentials(
            url=values["url"].get_secret_value() if values["url"] else None,
            api_key=values["apikey"].get_secret_value() if values["apikey"] else None,
            token=values["token"].get_secret_value() if values["token"] else None,
            password=(
                values["password"].get_secret_value() if values["password"] else None
            ),
            username=(
                values["username"].get_secret_value() if values["username"] else None
            ),
            instance_id=(
                values["instance_id"].get_secret_value()
                if values["instance_id"]
                else None
            ),
            version=values["version"].get_secret_value() if values["version"] else None,
            verify=values["verify"],
        )

        watsonx_chat = ModelInference(
            model_id=values["model_id"],
            deployment_id=values["deployment_id"],
            credentials=credentials,
            params=values["params"],
            project_id=values["project_id"],
            space_id=values["space_id"],
        )
        values["watsonx_model"] = watsonx_chat

        return values

    def _create_chat_prompt(self, messages: List[Dict[str, Any]]) -> str:
        prompt = ""

        if self.model_id in ["ibm/granite-13b-chat-v1", "ibm/granite-13b-chat-v2"]:
            for message in messages:
                if message["role"] == "system":
                    prompt += "<|system|>\n" + message["content"] + "\n\n"
                elif message["role"] == "assistant":
                    prompt += "<|assistant|>\n" + message["content"] + "\n\n"
                elif message["role"] == "function":
                    prompt += "<|function|>\n" + message["content"] + "\n\n"
                elif message["role"] == "tool":
                    prompt += "<|tool|>\n" + message["content"] + "\n\n"
                else:
                    prompt += "<|user|>:\n" + message["content"] + "\n\n"

            prompt += "<|assistant|>\n"

        elif self.model_id in [
            "meta-llama/llama-2-13b-chat",
            "meta-llama/llama-2-70b-chat",
        ]:
            for message in messages:
                if message["role"] == "system":
                    prompt += "[INST] <<SYS>>\n" + message["content"] + "<</SYS>>\n\n"
                elif message["role"] == "assistant":
                    prompt += message["content"] + "\n[INST]\n\n"
                else:
                    prompt += message["content"] + "\n[/INST]\n"

        elif self.model_id in ["meta-llama/llama-3-1-70b-instruct"]:
            for message in messages:
                if message["role"] == "system":
                    prompt += f"<|begin_of_text|><|start_header_id|>system<|end_header_id|>\n {message['content']} <|eot_id|>\n"
                elif message["role"] == "assistant":
                    (
                        prompt
                        + f"<|begin_of_text|><|start_header_id|>assistant<|end_header_id|>\n {message['content']} <|eot_id|>\n"
                    )
                elif message["role"] == "tool_call":
                    prompt += f"<|begin_of_text|><|start_header_id|>{message.get("role")}<|end_header_id|>\n {message['content']} <|eot_id|>\n"
                else:
                    prompt += f"<|begin_of_text|><|start_header_id|>user<|end_header_id|>\n {message['content']} <|eot_id|>\n"
            prompt += "<|assistant|>\n"
        else:
            prompt = ChatPromptValue(
                messages=convert_to_messages(messages) + [AIMessage(content="")]
            ).to_string()

        return prompt

    def _get_payload(
        self, inputs: Sequence[Dict], params: Sequence[Dict], **kwargs: Any
    ) -> dict:
        messages: List[Dict[str, Any]] = []
        for msg in inputs:
            if isinstance(msg, str):
                # (WFH) this shouldn't ever be reached but leaving this here bcs
                # it's a Chesterton's fence I'm unwilling to touch
                messages.append(dict(role="user", content=msg))
            elif isinstance(msg, dict):
                if msg.get("content", None) is None:
                    # content=None is valid for assistant messages (tool calling)
                    if not msg.get("role") == "assistant":
                        raise ValueError(f"Message {msg} has no content.")
                messages.append(msg)
            else:
                raise ValueError(f"Unknown message received: {msg} of type {type(msg)}")

        return {"messages": messages, **params}

    def _generate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        stream: Optional[bool] = None,
        **kwargs: Any,
    ) -> ChatResult:

        new_prompt = []
        system_message = ""
        message_dicts, params = self._create_message_dicts(messages, stop, **kwargs)
        chat_messages = []
        chat_messages.extend([m for m in message_dicts])

        tools = kwargs.get("tools")

        if tools:
            tool_descriptions = []
            for tool in tools:
                function_call = tool.get("function")
                args = function_call["parameters"]["properties"]
                tool_description = {
                    "name": function_call["name"],
                    "description": function_call["description"],
                    "args": args,
                }

                tool_descriptions.append(tool_description)

            prompt = f"""Given the following functions, please respond with a JSON for a function call with its proper arguments that best answers the given prompt.\n

            {tool_descriptions}\n

            Tools should use the following format:\n
            {TOOL_SCHEMA}

            Reminder:
                -Required parameters MUST be specified\
                -Put the entire function call reply on one line\
                -ONLY use the function arguments provided in the tool description.\
                -If you do not need to use a tool or you have the answer then respond directly to the user.
            """

            #### PREPROCESSING BEFORE SENDING TO LLM ####

            chat_messages.append({"role": "system", "content": re.sub(r"\s+", " ", prompt).strip()})

            for message in chat_messages:
                if message.get("role") == "system":
                    system_message += message.get("content")
                    system_message += "\n When responding to the user your role should always be assistant"

            new_prompt.insert(0, {"role": "system", "content": system_message})

            chat_messages = [message for message in chat_messages if message.get("role") != "system"]

            new_prompt.extend(chat_messages)

            if "tools" in kwargs:
                del kwargs["tools"]
            if "tool_choice" in kwargs:
                del kwargs["tool_choice"]

        response = self.watsonx_model.generate(
            prompt=json.dumps(new_prompt), **(kwargs | {"params": params})
        )

        #### POST PROCESSING AFTER RECEIVING RESPONSE FROM LLM ####

        return self._create_chat_result(response)

    def _create_message_dicts(
        self, messages: List[BaseMessage], stop: Optional[List[str]], **kwargs: Any
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        params = {**self.params} if self.params else {}
        params = params | {**kwargs.get("params", {})}
        if stop is not None:
            if params and "stop_sequences" in params:
                raise ValueError(
                    "`stop_sequences` found in both the input and default params."
                )
            params = (params or {}) | {"stop_sequences": stop}
        message_dicts = [_convert_message_to_dict(m) for m in messages]
        return message_dicts, params

    def _create_chat_result(self, response: Union[dict]) -> ChatResult:
        generations = []
        sum_of_total_generated_tokens = 0
        sum_of_total_input_tokens = 0
        call_id = uuid.uuid4().hex

        if response.get("error"):
            raise ValueError(response.get("error"))

        for res in response["results"]:

            message = _post_processing(res, call_id)
            generation_info = dict(finish_reason=res.get("stop_reason"))
            if "generated_token_count" in res:
                sum_of_total_generated_tokens += res["generated_token_count"]
            if "input_token_count" in res:
                sum_of_total_input_tokens += res["input_token_count"]
            total_token = sum_of_total_generated_tokens + sum_of_total_input_tokens
            if total_token and isinstance(message, AIMessage):
                message.usage_metadata = {
                    "input_tokens": sum_of_total_input_tokens,
                    "output_tokens": sum_of_total_generated_tokens,
                    "total_tokens": total_token,
                }
            gen = ChatGeneration(
                message=message,
                generation_info=generation_info,
            )
            generations.append(gen)
        token_usage = {
            "generated_token_count": sum_of_total_generated_tokens,
            "input_token_count": sum_of_total_input_tokens,
        }
        llm_output = {
            "token_usage": token_usage,
            "model_name": self.model_id,
            "system_fingerprint": response.get("system_fingerprint", ""),
        }
        return ChatResult(generations=generations, llm_output=llm_output)

    def bind_functions(
        self,
        functions: Sequence[Union[Dict[str, Any], Type[BaseModel], Callable, BaseTool]],
        function_call: Optional[
            Union[_FunctionCall, str, Literal["auto", "none"]]
        ] = None,
        **kwargs: Any,
    ) -> Runnable[LanguageModelInput, BaseMessage]:
        """Bind functions (and other objects) to this chat model.

        Assumes model is compatible with IBM watsonx.ai function-calling API.

        Args:
            functions: A list of function definitions to bind to this chat model.
                Can be  a dictionary, pydantic model, or callable. Pydantic
                models and callables will be automatically converted to
                their schema dictionary representation.
            function_call: Which function to require the model to call.
                Must be the name of the single provided function or
                "auto" to automatically determine which function to call
                (if any).
            **kwargs: Any additional parameters to pass to the
                :class:`~langchain.runnable.Runnable` constructor.
        """

        formatted_functions = [convert_to_openai_function(fn) for fn in functions]
        if function_call is not None:
            function_call = (
                {"name": function_call}
                if isinstance(function_call, str)
                and function_call not in ("auto", "none")
                else function_call
            )
            if isinstance(function_call, dict) and len(formatted_functions) != 1:
                raise ValueError(
                    "When specifying `function_call`, you must provide exactly one "
                    "function."
                )
            if (
                isinstance(function_call, dict)
                and formatted_functions[0]["name"] != function_call["name"]
            ):
                raise ValueError(
                    f"Function call {function_call} was specified, but the only "
                    f"provided function was {formatted_functions[0]['name']}."
                )
            kwargs = {**kwargs, "function_call": function_call}
        return super().bind(
            functions=formatted_functions,
            **kwargs,
        )

    def bind_tools(
        self,
        tools: Sequence[Union[Dict[str, Any], Type[BaseModel], Callable, BaseTool]],
        *,
        tool_choice: Optional[
            Union[dict, str, Literal["auto", "none", "any", "required"], bool]
        ] = None,
        **kwargs: Any,
    ) -> Runnable[LanguageModelInput, BaseMessage]:
        bind_tools_supported_models = ["meta-llama/llama-3-1-70b-instruct"]

        if self.model_id not in bind_tools_supported_models:
            raise Warning(
                f"bind_tools() method for ChatWatsonx support only "
                f"following models: {bind_tools_supported_models}"
            )
        formatted_tools = [convert_to_openai_tool(tool) for tool in tools]
        return super().bind(tools=formatted_tools, **kwargs)

    def with_structured_output(
        self,
        schema: Optional[Union[Dict, Type[BaseModel]]] = None,
        *,
        method: Literal["function_calling", "json_mode"] = "function_calling",
        include_raw: bool = False,
        **kwargs: Any,
    ) -> Runnable[LanguageModelInput, Union[Dict, BaseModel]]:
        if kwargs:
            raise ValueError(f"Received unsupported arguments {kwargs}")
        is_pydantic_schema = _is_pydantic_class(schema)
        if method == "function_calling":
            if schema is None:
                raise ValueError(
                    "schema must be specified when method is 'function_calling'. "
                    "Received None."
                )
            llm = self.bind_tools([schema], tool_choice=True)
            if is_pydantic_schema:
                output_parser: OutputParserLike = PydanticToolsParser(
                    tools=[schema],  # type: ignore[list-item]
                    first_tool_only=True,  # type: ignore[list-item]
                )
            else:
                key_name = convert_to_openai_tool(schema)["function"]["name"]
                output_parser = JsonOutputKeyToolsParser(
                    key_name=key_name, first_tool_only=True
                )
        elif method == "json_mode":
            llm = self.bind(response_format={"type": "json_object"})
            output_parser = (
                PydanticOutputParser(pydantic_object=schema)  # type: ignore[type-var, arg-type]
                if is_pydantic_schema
                else JsonOutputParser()
            )
        else:
            raise ValueError(
                f"Unrecognized method argument. Expected one of 'function_calling' or "
                f"'json_format'. Received: '{method}'"
            )

        if include_raw:
            parser_assign = RunnablePassthrough.assign(
                parsed=itemgetter("raw") | output_parser, parsing_error=lambda _: None
            )
            parser_none = RunnablePassthrough.assign(parsed=lambda _: None)
            parser_with_fallback = parser_assign.with_fallbacks(
                [parser_none], exception_key="parsing_error"
            )
            return RunnableMap(raw=llm) | parser_with_fallback
        else:
            return llm | output_parser


def _is_pydantic_class(obj: Any) -> bool:
    return isinstance(obj, type) and issubclass(obj, BaseModel)
