import copy
import re
from ast import literal_eval
from pathlib import Path
from typing import Union, Optional, Tuple, Callable, List, Literal
from collections.abc import Collection
import pandas as pd
import json
from colorama import Fore
from attr import attrs, attrib
import guidance

from blendsql.models import Model, LocalModel
from blendsql.ingredients.generate import generate, user, assistant
from blendsql._program import Program
from blendsql.ingredients.ingredient import QAIngredient
from blendsql.db.utils import single_quote_escape
from blendsql._exceptions import IngredientException
from blendsql.ingredients.utils import (
    initialize_retriever,
    cast_responses_to_datatypes,
    partialclass,
)
from blendsql._constants import ModifierType
from .examples import QAExample, AnnotatedQAExample

MAIN_INSTRUCTION = "Answer the question given the table context.\n"
LONG_ANSWER_INSTRUCTION = "Make the answer as concrete as possible, providing more context and reasoning using the entire table.\n"
SHORT_ANSWER_INSTRUCTION = "Keep the answers as short as possible, without leading context. For example, do not say 'The answer is 2', simply say '2'.\n"
DEFAULT_QA_FEW_SHOT: List[AnnotatedQAExample] = [
    AnnotatedQAExample(**d)
    for d in json.loads(
        open(Path(__file__).resolve().parent / "./default_examples.json", "r").read()
    )
]


class QAProgram(Program):
    def __call__(
        self,
        model: Model,
        current_example: QAExample,
        context_formatter: Callable[[pd.DataFrame], str],
        few_shot_examples: List[QAExample],
        list_options_in_prompt: bool = True,
        modifier: ModifierType = None,
        long_answer: Optional[bool] = False,
        max_tokens: Optional[int] = None,
        regex: Optional[str] = None,
        **kwargs,
    ) -> Tuple[str, str]:
        if isinstance(model, LocalModel):
            lm: guidance.models.Model = model.model_obj
        context_formatter(
            current_example.context
        ) if current_example.context is not None else ""
        options_alias_to_original = {}
        options = current_example.options
        if options is not None:
            # Since 'options' is a mutable list, create a copy to retain the originals
            options_with_aliases = copy.deepcopy(options)
            # Below we check to see if our options have a unique first word
            # sometimes, the model will generate 'Frank' instead of 'Frank Smith'
            # We still want to align that, in this case
            add_first_word = False
            if len(set([i.split(" ")[0] for i in options])) == len(options):
                add_first_word = True
            for option in options:
                option = str(option)
                for option_alias in [option.title(), option.upper()]:
                    options_with_aliases.add(option_alias)
                    options_alias_to_original[option_alias] = option
                if add_first_word:
                    option_alias = option.split(" ")[0]
                    options_alias_to_original[option_alias] = option
                    options_with_aliases.add(option_alias)
        if isinstance(model, LocalModel):
            with guidance.user():
                lm += MAIN_INSTRUCTION
                if long_answer:
                    lm += LONG_ANSWER_INSTRUCTION
                else:
                    lm += SHORT_ANSWER_INSTRUCTION
            for example in few_shot_examples:
                with guidance.user():
                    lm += example.to_string(context_formatter)
                with guidance.assistant():
                    lm += example.answer
            with guidance.user():
                lm += current_example.to_string(
                    context_formatter, list_options=list_options_in_prompt
                )
            prompt = lm._current_prompt()
            if options is not None:
                gen_f = guidance.select(
                    options=options_with_aliases,
                    list_append=bool(modifier is not None),
                    name="response",
                )
            else:
                gen_f = guidance.gen(
                    max_tokens=max_tokens or 200,
                    regex=regex,
                    list_append=bool(modifier is not None),
                    name="response",
                )
            # Parse the modifier arg
            if modifier is not None:
                if modifier == "*":
                    gen_f = guidance.zero_or_more(gen_f)
                elif modifier == "+":
                    gen_f = guidance.one_or_more(gen_f)
                elif re.match("{\d+}", modifier):
                    repeats = [
                        int(i)
                        for i in modifier.replace("}", "").replace("{", "").split(",")
                    ]
                    if len(repeats) == 1:
                        repeats = repeats * 2
                    min_length, max_length = repeats
                    gen_f = guidance.sequence(
                        gen_f, min_length=min_length, max_length=max_length
                    )
                else:
                    raise IngredientException(
                        f"Invalid modifier arg {modifier}\dValid values are '+', '*', or any string matching the '{{\d+}}' pattern"
                    )

            @guidance(stateless=True, dedent=False)
            def make_predictions(lm, gen_f) -> guidance.models.Model:
                with guidance.assistant():
                    lm += guidance.capture(gen_f, name="response")
                return lm

            response = (lm + make_predictions(gen_f))["response"]
        else:
            messages = []
            intro_prompt = MAIN_INSTRUCTION
            if long_answer:
                intro_prompt += LONG_ANSWER_INSTRUCTION
            else:
                intro_prompt += SHORT_ANSWER_INSTRUCTION
            messages.append(user(intro_prompt))
            # Add few-shot examples
            for example in few_shot_examples:
                messages.append(user(example.to_string(context_formatter)))
                messages.append(assistant(example.answer))
            # Add current question + context for inference
            messages.append(
                user(
                    current_example.to_string(
                        context_formatter, list_options=list_options_in_prompt
                    )
                )
            )
            response = generate(
                model,
                messages_list=[messages],
                max_tokens=max_tokens,
            )[0].strip()
            prompt = "".join([i["content"] for i in messages])
        if isinstance(response, str):
            # If we have specified a modifier, we try to parse it to a tuple
            if modifier:
                try:
                    response = literal_eval(response)
                    assert isinstance(response, list)
                    response = tuple(response)
                except (ValueError, SyntaxError, AssertionError):
                    response = [i.strip() for i in response.split(",")]
                    response = tuple(
                        [
                            "'{}'".format(single_quote_escape(val.strip()))
                            if isinstance(val, str)
                            else val
                            for val in cast_responses_to_datatypes(response)
                        ]
                    )
            else:
                response = cast_responses_to_datatypes([response])[0]
        # Map from modified options to original, as they appear in DB
        if isinstance(response, str):
            response = [response]
        response: List[str] = [options_alias_to_original.get(r, r) for r in response]
        if len(response) == 1 and not modifier:
            response = response[0]
            if options and response not in options:
                print(
                    Fore.RED
                    + f"Model did not select from a valid option!\nExpected one of {options}, got '{response}'"
                    + Fore.RESET
                )
            response = f"'{response}'"
        else:
            response = tuple(response)
        return (response, prompt)


@attrs
class LLMQA(QAIngredient):
    DESCRIPTION = """
    If mapping to a new column still cannot answer the question with valid SQL, turn to an end-to-end solution using the aggregate function:
        `{{LLMQA('question', (blendsql))}}`
        Optionally, this function can take an `options` argument to restrict its output to an existing SQL column.
        For example: `... WHERE column = {{LLMQA('question', (blendsql), options='table::column)}}`
    """
    model: Model = attrib(default=None)
    context_formatter: Callable[[pd.DataFrame], str] = attrib(
        default=lambda df: df.to_markdown(index=False)
    )
    list_options_in_prompt: bool = attrib(default=True)
    few_shot_retriever: Callable[[str], List[AnnotatedQAExample]] = attrib(default=None)
    k: Optional[int] = attrib(default=None)

    @classmethod
    def from_args(
        cls,
        model: Optional[Model] = None,
        few_shot_examples: Optional[List[dict]] = None,
        context_formatter: Callable[[pd.DataFrame], str] = lambda df: df.to_markdown(
            index=False
        ),
        list_options_in_prompt: bool = True,
        k: Optional[int] = None,
    ):
        """Creates a partial class with predefined arguments.

        Args:
            few_shot_examples: A list of Example dictionaries for few-shot learning.
                If not specified, will use [default_examples.json](https://github.com/parkervg/blendsql/blob/main/blendsql/ingredients/builtin/qa/default_examples.json) as default.
            context_formatter: A callable that formats a pandas DataFrame into a string.
                Defaults to a lambda function that converts the DataFrame to markdown without index.
             k: Determines number of few-shot examples to use for each ingredient call.
                Default is None, which will use all few-shot examples on all calls.
                If specified, will initialize a haystack-based DPR retriever to filter examples.

        Returns:
            Type[QAIngredient]: A partial class of QAIngredient with predefined arguments.

        Examples:
            ```python
            from blendsql import blend, LLMQA
            from blendsql.ingredients.builtin import DEFAULT_QA_FEW_SHOT

            ingredients = {
                LLMQA.from_args(
                    few_shot_examples=[
                        *DEFAULT_QA_FEW_SHOT,
                        {
                            "question": "Which weighs the most?",
                            "context": {
                                {
                                    "Animal": ["Dog", "Gorilla", "Hamster"],
                                    "Weight": ["20 pounds", "350 lbs", "100 grams"]
                                }
                            },
                            "answer": "Gorilla",
                            # Below are optional
                            "options": ["Dog", "Gorilla", "Hamster"]
                        }
                    ],
                    # Will fetch `k` most relevant few-shot examples using embedding-based retriever
                    k=2,
                    # Lambda to turn the pd.DataFrame to a serialized string
                    context_formatter=lambda df: df.to_markdown(
                        index=False
                    )
                )
            }
            smoothie = blend(
                query=blendsql,
                db=db,
                ingredients=ingredients,
                default_model=model,
            )
            ```
        """
        if few_shot_examples is None:
            few_shot_examples = DEFAULT_QA_FEW_SHOT
        else:
            few_shot_examples = [
                AnnotatedQAExample(**d) if isinstance(d, dict) else d
                for d in few_shot_examples
            ]
        few_shot_retriever = initialize_retriever(
            examples=few_shot_examples, k=k, context_formatter=context_formatter
        )
        return partialclass(
            cls,
            model=model,
            few_shot_retriever=few_shot_retriever,
            context_formatter=context_formatter,
            list_options_in_prompt=list_options_in_prompt,
        )

    def run(
        self,
        model: Model,
        question: str,
        context_formatter: Callable[[pd.DataFrame], str],
        few_shot_retriever: Callable[[str], List[AnnotatedQAExample]] = None,
        options: Optional[Collection[str]] = None,
        list_options_in_prompt: bool = None,
        modifier: ModifierType = None,
        output_type: Optional[
            Literal[
                "integer",
                "float",
                "string",
                "boolean",
                "List[integer]",
                "List[float]",
                "List[string]",
                "List[boolean]",
            ]
        ] = None,
        regex: Optional[str] = None,
        context: Optional[pd.DataFrame] = None,
        value_limit: Optional[int] = None,
        long_answer: bool = False,
        **kwargs,
    ) -> Union[str, int, float]:
        """
        Args:
            question: The question to map onto the values. Will also be the new column name
            context: Table subset to use as context in answering question
            model: The Model (blender) we will make calls to.
            context_formatter: Callable defining how we want to serialize table context.
            few_shot_retriever: Callable which takes a string, and returns n most similar few-shot examples
            options: Optional collection with which we try to constrain generation.
            list_options_in_prompt: Defines whether we include options in the prompt for the current inference example
            modifier: If we expect an array of scalars, this defines the regex we want to apply.
                Used directly for constrained decoding at inference time if we have a guidance model.
            output_type: In the absence of example_outputs, give the Model some signal as to what we expect as output.
            regex: Optional regex to constrain answer generation.
            value_limit: Optional limit on how many rows from context we use
            long_answer: If true, we more closely mimic long-form end-to-end question answering.
                If false, we just give the answer with no explanation or context

        Returns:
            Union[str, int, float, tuple] containing the response from the model.
                Response will only be a tuple if `modifier` is not None.
        """
        if model is None:
            raise IngredientException(
                "LLMQA requires a `Model` object, but nothing was passed!\nMost likely you forgot to set the `default_model` argument in `blend()`"
            )
        if few_shot_retriever is None:
            few_shot_retriever = lambda *_: DEFAULT_QA_FEW_SHOT
        if context is not None:
            if value_limit is not None:
                context = context.iloc[:value_limit]
        resolved_output_type = None
        if modifier and output_type:
            if not output_type.startswith("List"):
                resolved_output_type = f"List[{output_type}]"
        elif modifier:
            resolved_output_type = "list"
        else:
            resolved_output_type = output_type
        current_example = QAExample(
            **{
                "question": question,
                "context": context,
                "options": options,
                "output_type": resolved_output_type,
            }
        )
        few_shot_examples: List[AnnotatedQAExample] = few_shot_retriever(
            current_example.to_string(context_formatter)
        )
        result = model.predict(
            program=QAProgram,
            current_example=current_example,
            context_formatter=context_formatter,
            few_shot_examples=few_shot_examples,
            list_options_in_prompt=list_options_in_prompt,
            modifier=modifier,
            long_answer=long_answer,
            regex=regex,
            **kwargs,
        )
        return result
