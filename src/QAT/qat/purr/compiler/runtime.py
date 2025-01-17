# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2023 Oxford Quantum Circuits Ltd
from collections import Iterable
from numbers import Number
from typing import List, Optional, TypeVar, Union

import numpy
from qat.purr.compiler.builders import InstructionBuilder, QuantumInstructionBuilder
from qat.purr.compiler.config import CalibrationArguments, MetricsType, ResultsFormatting
from qat.purr.compiler.execution import (
    InstructionExecutionEngine,
    QuantumExecutionEngine,
    _binary,
)
from qat.purr.compiler.hardware_models import QuantumHardwareModel
from qat.purr.compiler.instructions import Instruction, is_generated_name
from qat.purr.compiler.metrics import CompilationMetrics, MetricsMixin
from qat.purr.utils.logger import get_default_logger

log = get_default_logger()


class RemoteCalibration:
    """
    Base class for any remote calibration executions. These are far more complicated
    blocks than purely a string of instructions and include nested executions and rely
    on classic Python code.
    """
    def run(
        self,
        model: QuantumHardwareModel,
        runtime: "QuantumRuntime",
        args: CalibrationArguments
    ):
        raise ValueError("Calibration cannot be run at this time.")

    def arguments_type(self) -> type:
        """ Returns the type of this calibrations arguments. """
        return CalibrationArguments


class QuantumExecutableBlock:
    """ Generic executable block that can be run on a quantum runtime. """
    def run(self, runtime: "QuantumRuntime"):
        pass


class CalibrationWithArgs(QuantumExecutableBlock):
    """ Wrapper for a calibration and argument combination. """
    def __init__(self, calibration: RemoteCalibration, args: CalibrationArguments = None):
        self.calibration = calibration
        self.args = args or CalibrationArguments()

    def run(self, runtime: "QuantumRuntime"):
        if self.calibration is None:
            raise ValueError("No calibration to run.")

        self.calibration.run(runtime.model, runtime, self.args)


AnyEngine = TypeVar('AnyEngine', bound=InstructionExecutionEngine, covariant=True)


class QuantumRuntime(MetricsMixin):
    def __init__(self, execution_engine: InstructionExecutionEngine, metrics=None):
        super().__init__()
        self.engine: AnyEngine = execution_engine
        self.compilation_metrics = metrics or CompilationMetrics()

    @property
    def model(self):
        return self.engine.model if self.engine is not None else None

    def _transform_results(
        self, results, format_flags: ResultsFormatting, repeats: Optional[int] = None
    ):
        """
        Transform the raw results into the format that we've been asked to provide. Look
        at individual transformation documentation for descriptions on what they do.
        """
        if len(results) == 0:
            return []

        # If we have no flags at all just infer structure simplification.
        if format_flags is None:
            format_flags = ResultsFormatting.DynamicStructureReturn

        if repeats is None:
            repeats = 1000

        def simplify_results(simplify_target):
            """
            To facilitate backwards compatability and being able to run low-level
            experiments alongside quantum programs we make some assumptions based upon
            form of the results.

            If all results have default variable names then the user didn't care about
            value assignment or this was a low-level experiment - in both cases, it
            means we can throw away the names and simply return the results in the order
            they were defined in the instructions.

            If we only have one result after this, just return that list directly
            instead, as it's probably just a single experiment.
            """
            if all([is_generated_name(k) for k in simplify_target.keys()]):
                if len(simplify_target) == 1:
                    return list(simplify_target.values())[0]
                else:
                    squashed_results = list(simplify_target.values())
                    if all(isinstance(val, numpy.ndarray) for val in squashed_results):
                        return numpy.array(squashed_results)
                    return squashed_results
            else:
                return simplify_target

        if ResultsFormatting.BinaryCount in format_flags:
            results = {key: _binary_count(val, repeats) for key, val in results.items()}

        def squash_binary(value):
            if isinstance(value, int):
                return str(value)
            elif all(isinstance(val, int) for val in value):
                return ''.join([str(val) for val in value])

        if ResultsFormatting.SquashBinaryResultArrays in format_flags:
            results = {key: squash_binary(val) for key, val in results.items()}

        # Dynamic structure return is an ease-of-use flag to strip things that you know
        # your use-case won't use, such as variable names and nested lists.
        if ResultsFormatting.DynamicStructureReturn in format_flags:
            results = simplify_results(results)

        return results

    def run_calibration(
        self, calibrations: Union[CalibrationWithArgs, List[CalibrationWithArgs]]
    ):
        """ Make 'calibration' distinct from 'quantum executable' for usabilities sake. """
        self.run_quantum_executable(calibrations)

    def run_quantum_executable(
        self, executables: Union[QuantumExecutableBlock, List[QuantumExecutableBlock]]
    ):
        if executables is None:
            return

        if not isinstance(executables, list):
            executables = [executables]

        for exe in executables:
            exe.run(self)

    def execute(self, instructions, results_format=None, repeats=None):
        """
        Executes these instructions against the current engine and returns the results.
        """
        if self.engine is None:
            raise ValueError("No execution engine available.")

        if isinstance(instructions, InstructionBuilder):
            instructions = instructions.instructions

        if instructions is None or not any(instructions):
            raise ValueError(
                "No instructions passed to the process or stored for execution."
            )

        instructions = self.engine.optimize(instructions)
        self.engine.validate(instructions)
        self.record_metric(
            MetricsType.OptimizedInstructionCount, opt_inst_count := len(instructions)
        )
        log.info(f"Optimized instruction count: {opt_inst_count}")

        results = self.engine.execute(instructions)
        return self._transform_results(results, results_format, repeats)


def _binary_count(results_list, repeats):
    """
    Returns a dictionary of binary number: count. So for a two qubit register it'll return the various counts for
    ``00``, ``01``, ``10`` and ``11``.
    """
    def flatten(res):
        """
        Combine binary result from the QPU into composite key result.
        Aka '0110' or '0001'
        """
        if isinstance(res, Iterable):
            return ''.join([flatten(val) for val in res])
        else:
            return str(res)

    def get_tuple(res, index):
        return [
            val[index] if isinstance(val, (List, numpy.ndarray)) else val for val in res
        ]

    binary_results = _binary(results_list)

    # If our results are a single qubit then pretend to be a register of one.
    if isinstance(next(iter(binary_results), None),
                  Number) and len(binary_results) == repeats:
        binary_results = [binary_results]

    result_count = dict()
    for qubit_result in [list(get_tuple(binary_results, i)) for i in range(repeats)]:
        key = flatten(qubit_result)
        value = result_count.get(key, 0)
        result_count[key] = value + 1

    return result_count


def get_model(hardware: Union[QuantumExecutionEngine, QuantumHardwareModel]):
    if isinstance(hardware, QuantumExecutionEngine):
        return hardware.model
    return hardware


def get_runtime(hardware: Union[QuantumExecutionEngine, QuantumHardwareModel]):
    if isinstance(hardware, QuantumExecutionEngine):
        return QuantumRuntime(hardware)
    elif isinstance(hardware, QuantumHardwareModel):
        default_hw = hardware.get_engine()
        if default_hw is None:
            raise ValueError(
                f"{str(hardware)} is not mapped to a recognized execution engine."
            )
        return QuantumRuntime(default_hw(hardware))

    raise ValueError(
        f"{str(hardware)} is not a recognized hardware model or execution engine."
    )


def get_builder(
    model: Union[QuantumHardwareModel, QuantumExecutionEngine]
) -> QuantumInstructionBuilder:
    if isinstance(model, QuantumExecutionEngine):
        model = model.model
    default_builder = model.get_builder()
    if default_builder is None:
        raise ValueError(f"{str(model)} is not mapped to a recognized instruction builder.")

    return default_builder(model)


def execute_instructions(
    hardware: Union[QuantumExecutionEngine, QuantumHardwareModel],
    instructions: Union[List[Instruction], QuantumInstructionBuilder],
    results_format=None,
    executable_blocks: List[QuantumExecutableBlock] = None,
    repeats: Optional[int] = None
):
    active_runtime = get_runtime(hardware)

    active_runtime.run_quantum_executable(executable_blocks)
    return (
        active_runtime.execute(instructions, results_format, repeats),
        active_runtime.compilation_metrics
    )
