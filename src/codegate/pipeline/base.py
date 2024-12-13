import dataclasses
import datetime
import json
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

import structlog
from litellm import ChatCompletionRequest, ModelResponse
from pydantic import BaseModel

from codegate.db.models import Alert, Output, Prompt
from codegate.pipeline.secrets.manager import SecretsManager

logger = structlog.get_logger("codegate")


@dataclass
class CodeSnippet:
    """
    Represents a code snippet with its programming language.

    Args:
        language: The programming language identifier (e.g., 'python', 'javascript')
        code: The actual code content
    """

    code: str
    language: Optional[str]
    filepath: Optional[str]
    libraries: List[str] = field(default_factory=list)

    def __post_init__(self):
        if self.language is not None:
            self.language = self.language.strip().lower()


class AlertSeverity(Enum):
    INFO = "info"
    CRITICAL = "critical"


@dataclass
class PipelineSensitiveData:
    manager: SecretsManager
    session_id: str
    api_key: Optional[str] = None
    model: Optional[str] = None
    provider: Optional[str] = None
    api_base: Optional[str] = None

    def secure_cleanup(self):
        """Securely cleanup sensitive data for this session"""
        if self.manager is None or self.session_id == "":
            return

        self.manager.cleanup_session(self.session_id)
        self.session_id = ""

        # Securely wipe the API key using the same method as secrets manager
        if self.api_key is not None:
            api_key_bytes = bytearray(self.api_key.encode())
            self.manager.crypto.wipe_bytearray(api_key_bytes)
            self.api_key = None

        self.model = None


@dataclass
class PipelineContext:
    code_snippets: List[CodeSnippet] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    sensitive: Optional[PipelineSensitiveData] = field(default_factory=lambda: None)
    alerts_raised: List[Alert] = field(default_factory=list)
    prompt_id: Optional[str] = field(default_factory=lambda: None)
    input_request: Optional[Prompt] = field(default_factory=lambda: None)
    output_responses: List[Output] = field(default_factory=list)

    def add_code_snippet(self, snippet: CodeSnippet):
        self.code_snippets.append(snippet)

    def get_snippets_by_language(self, language: str) -> List[CodeSnippet]:
        return [s for s in self.code_snippets if s.language.lower() == language.lower()]

    def add_alert(
        self,
        step_name: str,
        severity_category: AlertSeverity = AlertSeverity.INFO,
        code_snippet: Optional[CodeSnippet] = None,
        trigger_string: Optional[str] = None,
    ) -> None:
        """
        Add an alert to the pipeline step alerts_raised.
        """
        if self.prompt_id is None:
            self.prompt_id = str(uuid.uuid4())

        if not code_snippet and not trigger_string:
            logger.warning("No code snippet or trigger string provided for alert. Will not create")
            return

        code_snippet_str = json.dumps(dataclasses.asdict(code_snippet)) if code_snippet else None

        self.alerts_raised.append(
            Alert(
                id=str(uuid.uuid4()),
                prompt_id=self.prompt_id,
                code_snippet=code_snippet_str,
                trigger_string=trigger_string,
                trigger_type=step_name,
                trigger_category=severity_category.value,
                timestamp=datetime.datetime.now(datetime.timezone.utc),
            )
        )
        logger.debug(f"Added alert to context: {self.alerts_raised[-1]}")

    def add_input_request(
        self, normalized_request: ChatCompletionRequest, is_fim_request: bool, provider: str
    ) -> None:
        try:
            if self.prompt_id is None:
                self.prompt_id = str(uuid.uuid4())

            request_str = json.dumps(normalized_request)

            self.input_request = Prompt(
                id=self.prompt_id,
                timestamp=datetime.datetime.now(datetime.timezone.utc),
                provider=provider,
                type="fim" if is_fim_request else "chat",
                request=request_str,
            )
            logger.debug(f"Added input request to context: {self.input_request}")
        except Exception as e:
            logger.warning(f"Failed to serialize input request: {normalized_request}", error=str(e))

    def add_output(self, model_response: ModelResponse) -> None:
        try:
            if self.prompt_id is None:
                logger.warning(f"Tried to record output without response: {model_response}")
                return

            if isinstance(model_response, BaseModel):
                output_str = model_response.model_dump_json(exclude_none=True, exclude_unset=True)
            else:
                output_str = json.dumps(model_response)

            self.output_responses.append(
                Output(
                    id=str(uuid.uuid4()),
                    prompt_id=self.prompt_id,
                    timestamp=datetime.datetime.now(datetime.timezone.utc),
                    output=output_str,
                )
            )
            logger.debug(f"Added output to context: {self.output_responses[-1]}")
        except Exception as e:
            logger.error(f"Failed to serialize output: {model_response}", error=str(e))
            return


@dataclass
class PipelineResponse:
    """Response generated by a pipeline step"""

    content: str
    step_name: str  # The name of the pipeline step that generated this response
    model: str  # Taken from the original request's model field


@dataclass
class PipelineResult:
    """
    Represents the result of a pipeline operation.
    Either contains a modified request to continue processing,
    or a response to return to the client.
    """

    request: Optional[ChatCompletionRequest] = None
    response: Optional[PipelineResponse] = None
    context: Optional[PipelineContext] = None
    error_message: Optional[str] = None

    def shortcuts_processing(self) -> bool:
        """Returns True if this result should end pipeline processing"""
        return self.response is not None or self.error_message is not None

    @property
    def success(self) -> bool:
        """Returns True if the pipeline step completed without errors"""
        return self.error_message is None


class PipelineStep(ABC):
    """Base class for all pipeline steps in the processing chain."""

    @property
    @abstractmethod
    def name(self) -> str:
        """
        Returns the name of the pipeline step.

        Returns:
            str: A unique identifier for this pipeline step
        """
        pass

    @staticmethod
    def get_last_user_message(
        request: ChatCompletionRequest,
    ) -> Optional[tuple[str, int]]:
        """
        Get the last user message and its index from the request.

        Args:
            request (ChatCompletionRequest): The chat completion request to process

        Returns:
            Optional[tuple[str, int]]: A tuple containing the message content and
                                       its index, or None if no user message is found
        """
        if request.get("messages") is None:
            return None
        for i in reversed(range(len(request["messages"]))):
            if request["messages"][i]["role"] == "user":
                content = request["messages"][i]["content"]
                return content, i

        return None

    @staticmethod
    def get_last_user_message_idx(request: ChatCompletionRequest) -> int:
        if request.get("messages") is None:
            return -1

        for idx, message in reversed(list(enumerate(request["messages"]))):
            if message.get("role", "") == "user":
                return idx

        return -1

    @staticmethod
    def get_all_user_messages(request: ChatCompletionRequest) -> str:
        all_user_messages = ""

        for message in request.get("messages", []):
            if message["role"] == "user":
                all_user_messages += "\n" + message["content"]

        return all_user_messages

    @abstractmethod
    async def process(
        self, request: ChatCompletionRequest, context: PipelineContext
    ) -> PipelineResult:
        """Process a request and return either modified request or response stream"""
        pass


class InputPipelineInstance:
    def __init__(
        self, pipeline_steps: List[PipelineStep], secret_manager: SecretsManager, is_fim: bool
    ):
        self.pipeline_steps = pipeline_steps
        self.secret_manager = secret_manager
        self.is_fim = is_fim
        self.context = PipelineContext()

    async def process_request(
        self,
        request: ChatCompletionRequest,
        provider: str,
        model: str,
        api_key: Optional[str] = None,
        api_base: Optional[str] = None,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> PipelineResult:
        """Process a request through all pipeline steps"""
        self.context.sensitive = PipelineSensitiveData(
            manager=self.secret_manager,
            session_id=str(uuid.uuid4()),
            api_key=api_key,
            model=model,
            provider=provider,
            api_base=api_base,
        )
        self.context.metadata["extra_headers"] = extra_headers
        current_request = request

        for step in self.pipeline_steps:
            result = await step.process(current_request, self.context)
            if result is None:
                continue

            if result.shortcuts_processing():
                # Also record the input when shortchutting
                self.context.add_input_request(
                    current_request, is_fim_request=self.is_fim, provider=provider
                )
                return result

            if result.request is not None:
                current_request = result.request

            if result.context is not None:
                self.context = result.context

        # Create the input request at the end so we make sure the secrets are obfuscated
        self.context.add_input_request(
            current_request, is_fim_request=self.is_fim, provider=provider
        )
        return PipelineResult(request=current_request, context=self.context)


class SequentialPipelineProcessor:
    def __init__(
        self, pipeline_steps: List[PipelineStep], secret_manager: SecretsManager, is_fim: bool
    ):
        self.pipeline_steps = pipeline_steps
        self.secret_manager = secret_manager
        self.is_fim = is_fim

    def create_instance(self) -> InputPipelineInstance:
        """Create a new pipeline instance for processing a request"""
        return InputPipelineInstance(self.pipeline_steps, self.secret_manager, self.is_fim)

    async def process_request(
        self,
        request: ChatCompletionRequest,
        provider: str,
        model: str,
        api_key: Optional[str] = None,
        api_base: Optional[str] = None,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> PipelineResult:
        """Create a new pipeline instance and process the request"""
        instance = self.create_instance()
        return await instance.process_request(
            request, provider, model, api_key, api_base, extra_headers
        )
