# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2023 Oxford Quantum Circuits Ltd
from __future__ import annotations

import re
from enum import Enum
from typing import Any, Dict, List, Set, Union

import numpy as np
from qat.purr.compiler.config import InlineResultsProcessing
from qat.purr.compiler.devices import PulseChannel, PulseShapeType, QuantumComponent, Qubit


def _stringify_qubits(qubits):
    return ','.join([str(qb) for qb in qubits])


class PostProcessType(Enum):
    DOWN_CONVERT = "down_convert"
    MEAN = "mean"
    LINEAR_MAP_COMPLEX_TO_REAL = "linear_map_real"
    DISCRIMINATE = "discriminate"
    MUL = "mul"

    def __repr__(self):
        return self.name


class ProcessAxis(Enum):
    TIME = "time"
    SEQUENCE = "sequence"

    def __repr__(self):
        return self.name


class Instruction:
    pass


class QuantumMetadata(Instruction):
    pass


class QuantumInstruction(Instruction):
    """
    Any node that deals particularly with quantum operations. All quantum operations
    must have some sort of target on the quantum computer, such as a qubit, channel, or
    another form of component.
    """
    def __init__(
        self, quantum_targets: Union[QuantumComponent, List[QuantumComponent]] = None
    ):
        if quantum_targets is None:
            quantum_targets = []
        elif not isinstance(quantum_targets, List):
            quantum_targets = [quantum_targets]

        invalid_targets = [
            val for val in quantum_targets if not isinstance(val, QuantumComponent)
        ]
        if any(invalid_targets):
            invalid_targets_str = ','.join([str(val) for val in invalid_targets])
            raise ValueError(f"Invalid targets for component: {invalid_targets_str}")

        # Quick way to make sure the targets are unique.
        self.quantum_targets: List[QuantumComponent] = []
        for tar in quantum_targets:
            if tar not in self.quantum_targets:
                self.quantum_targets.append(tar)

    @property
    def duration(self):
        return 0.0


class Repeat(Instruction):
    """
    Global meta-instruction that applies to the entire list of instructions. Repeat
    value of the current operations, also known as shots.
    """
    def __init__(self, repeat_count, repetition_period=None):
        super().__init__()
        self.repeat_count = repeat_count
        self.repetition_period = repetition_period or 100e-6

    def __repr__(self):
        return f"repeat {self.repeat_count},{self.repetition_period}"


class PhaseShift(QuantumInstruction):
    def __init__(self, channel: "PulseChannel", phase: float):
        super().__init__(channel)
        self.phase: float = phase

    @property
    def channel(self) -> "PulseChannel":
        return self.quantum_targets[0]

    def __repr__(self):
        return f"phaseshift {self.channel},{self.phase}"


class FrequencyShift(QuantumInstruction):
    def __init__(self, channel: "PulseChannel", frequency: float):
        super().__init__(channel)
        self.frequency: float = frequency

    @property
    def channel(self) -> "PulseChannel":
        return self.quantum_targets[0]

    def __repr__(self):
        return f"frequencyshift {self.channel},{self.frequency}"


class Id(QuantumInstruction):
    """ Simply a no-op, called an Identity gate. """
    def __repr__(self):
        return "id"


class AcquireMode(Enum):
    RAW = "raw"
    SCOPE = "scope"
    INTEGRATOR = "integrator"

    def __repr__(self):
        return self.name


class Delay(QuantumInstruction):
    def __init__(self, quantum_target, time: float):
        super().__init__(quantum_target)
        self.time: float = time

    @property
    def duration(self):
        return self.time

    def __repr__(self):
        return f"delay {str(self.time)}"


class Synchronize(QuantumInstruction):
    """
    Tells the QPU to wait for all the related channels to be free before continuing
    execution on any of them.
    """
    def __init__(
        self, sync_channels: Union[Qubit, PulseChannel, List[Union[Qubit, PulseChannel]]]
    ):
        super().__init__()
        self.add_channels(sync_channels)

    def add_channels(self, sync_channels):
        if not isinstance(sync_channels, List):
            sync_channels = [sync_channels]

        for target in [
            chan for val in sync_channels
            for chan in (val.get_all_channels() if isinstance(val, Qubit) else [val])
        ]:
            if not isinstance(target, PulseChannel):
                raise ValueError(
                    f"Attempted to try and add non PulseChannel ({target}) to a sync."
                )

            if target not in self.quantum_targets:
                self.quantum_targets.append(target)

    def __add__(self, other):
        if isinstance(other, Synchronize):
            return Synchronize(self.quantum_targets + other.quantum_targets)

        new_sync = Synchronize(self.quantum_targets)
        new_sync.add_channels(other)
        return new_sync

    def __repr__(self):
        return f"sync {','.join(name.id for name in self.quantum_targets)}"


class Assign(Instruction):
    """
    Assigns the variable 'x' the value 'y'. This can be performed as a part of running
    on the QPU or by a post-processing pass.
    """
    def __init__(self, name, value):
        self.name = name
        self.value = value

    def __repr__(self):
        return f"{self.name} = {str(self.value)}"


class Waveform(QuantumInstruction):
    @property
    def channel(self) -> "PulseChannel":
        return self.quantum_targets[0]


class CustomPulse(Waveform):
    """
    Send a pulse down this particular channel.
    """
    def __init__(
        self,
        quantum_target: "PulseChannel",
        samples: List[np.complex],
        ignore_channel_scale: bool = False
    ):
        super().__init__(quantum_target)
        self.samples: List[np.complex] = samples
        self.ignore_channel_scale: bool = ignore_channel_scale

    @property
    def duration(self):
        return len(self.samples) * self.channel.sample_time

    def __repr__(self):
        id_ = self.channel.full_id()
        duration = self.duration
        return f"custom pulse {id_},{duration}"


class Pulse(Waveform):
    """
    Send a pulse down this particular channel.
    """
    def __init__(
        self,
        quantum_target: "PulseChannel",
        shape: PulseShapeType,
        width: float,
        amp: float = 1.0,
        phase: float = 0.0,
        drag: float = 0.0,
        rise=0.0,
        amp_setup: float = 0.0,
        scale_factor: float = 1.0,
        zero_at_edges: int = 0,
        beta: float = 0.0,
        frequency: float = 0.0,
        internal_phase: float = 0.0,
        std_dev: float = 0.0,
        square_width: float = 0.0,
        ignore_channel_scale: bool = False,
    ):
        super().__init__(quantum_target)
        self.shape = shape
        self.width = width
        self.amp = amp
        self.phase = phase
        self.drag = drag
        self.rise = rise
        self.amp_setup = amp_setup
        self.scale_factor = scale_factor
        self.zero_at_edges = bool(zero_at_edges)
        self.beta = beta
        self.frequency = frequency
        self.internal_phase = internal_phase
        self.std_dev = std_dev
        self.square_width = square_width
        self.ignore_channel_scale = ignore_channel_scale

    @property
    def duration(self):
        return self.width

    def __repr__(self):
        return f"pulse {self.channel.full_id()},{self.shape.value},{self.amp}," \
            f"{self.phase},{self.width},{self.drag},{self.rise}"


class MeasurePulse(Pulse):
    pass


class DrivePulse(Pulse):
    pass


class SecondStatePulse(Pulse):
    pass


class CrossResonancePulse(Pulse):
    pass


class CrossResonanceCancelPulse(Pulse):
    pass


class Acquire(QuantumComponent, QuantumInstruction):
    suffix_incrementor: int = 0

    def __init__(
        self,
        channel: "PulseChannel",
        time: float = None,
        mode: AcquireMode = None,
        output_variable=None,
        existing_names: Set[str] = None,
        delay=180e-9,
        filter: Pulse = None
    ):
        super().__init__(channel.full_id())
        super(QuantumComponent, self).__init__(channel)
        self.time: float = time or 1.0e-6
        self.mode: AcquireMode = mode or AcquireMode.RAW
        self.delay = delay
        self.filter: Pulse = filter
        self.output_variable = output_variable or self.generate_name(existing_names)

        if filter is not None:
            if not isinstance(filter, Pulse):
                raise ValueError(
                    "Filter on an acquire has to be a Pulse. Instead it's a "
                    f"{type(filter)}"
                )

            if filter.duration != self.time:
                raise ValueError(
                    f"Filter duration '{filter.duration}' must be equal to Acquire "
                    f"duration '{self.time}'."
                )

    def generate_name(self, existing_names=None):
        return build_generated_name(existing_names, f"{self.channel.id}")

    @property
    def duration(self):
        return self.time

    @property
    def channel(self) -> "PulseChannel":
        return next(iter(self.quantum_targets), None)

    def __repr__(self):
        out_var = f"->{self.output_variable}" \
            if self.output_variable is not None else ""
        mode = f",{self.mode.value}" if self.mode is not None else ""
        return f"acquire {self.channel.full_id()},{self.time}{mode}{out_var}"


class PostProcessing(QuantumInstruction):
    """
    States what post-processing should happen after data has been acquired. This can
    happen in the FPGA's or a software post-process.
    """
    def __init__(self, acquire: Acquire, process, axes=None, args=None):
        super().__init__(acquire)
        if axes is not None and not isinstance(axes, List):
            axes = [axes]

        self.process: PostProcessType = process
        self.args: List = args or []
        self.axes: List[ProcessAxis] = axes or []
        self.output_variable = acquire.output_variable
        self.result_needed = False

    @property
    def acquire(self) -> Acquire:
        return self.quantum_targets[0]

    def __repr__(self):
        axis = ','.join([axi.value for axi in self.axes])
        args = f",{','.join(str(arg) for arg in self.args)}" if len(self.args) > 0 else ","
        output_var = f"->{self.output_variable}" if self.output_variable is not None else ""
        return f"{self.process.value} {self.acquire.output_variable}{args}{axis}{output_var}"


class Reset(QuantumInstruction):
    """ Resets this qubit to its starting state. """
    def __init__(self, qubit: Union[List[Qubit], Qubit]):
        if not isinstance(qubit, List):
            qubit = [qubit]

        invalid_reset_targets = [
            str(val) for val in qubit if not isinstance(val, (Qubit, PulseChannel))
        ]
        if any(invalid_reset_targets):
            raise ValueError(
                "Tried to reset on non-qubit/pulse channel "
                f"{', '.join(invalid_reset_targets)}."
            )

        super().__init__([
            val.get_drive_channel() if isinstance(val, Qubit) else val for val in qubit
        ])

    def __repr__(self):
        return f"reset {','.join([str(qb) for qb in self.quantum_targets])}"


class PhaseReset(QuantumInstruction):
    """
    Reset the phase shift of all the channels
    """
    quantum_targets: List[PulseChannel]

    def __init__(
        self, reset_channels: Union[Qubit, PulseChannel, List[Union[Qubit, PulseChannel]]]
    ):
        super().__init__()
        self.add_channels(reset_channels)

    def add_channels(
        self, reset_channels: Union[Qubit, PulseChannel, List[Union[Qubit, PulseChannel]]]
    ):
        if not isinstance(reset_channels, List):
            reset_channels = [reset_channels]

        for target in [
            chan for val in reset_channels
            for chan in (val.get_all_channels() if isinstance(val, Qubit) else [val])
        ]:
            if not isinstance(target, PulseChannel):
                raise ValueError(
                    f"Attempted to try and add non PulseChannel ({target}) to a sync."
                )

            if target not in self.quantum_targets:
                self.quantum_targets.append(target)

    def __add__(self, other):
        if isinstance(other, PhaseReset):
            return PhaseReset(self.quantum_targets + other.quantum_targets)

        new_reset = PhaseReset(self.quantum_targets)
        new_reset.add_channels(other)
        return new_reset

    def __repr__(self):
        return f"phase reset {','.join(name.id for name in self.quantum_targets)}"


class Return(Instruction):
    """ A statement defining what to return from a quantum execution. """
    def __init__(self, variables: List[str] = None):
        if variables is None:
            variables = []

        if not isinstance(variables, List):
            variables = [variables]

        self.variables = variables

    def __repr__(self):
        return f"return {','.join(self.variables)}"


class SweepOperation:
    """ Common parent for all things that need differentiating during a sweep. """
    pass


class SweepValue(SweepOperation):
    def __init__(self, name, value):
        self.name = name
        self.value = value


class DeviceUpdate(QuantumInstruction):
    """
    Dynamically assigns a value to a particular symbol or hardware attribute during
    execution.

    .. note:: It's still unknown how this will be represented in the instructions themselves, but that'll come later.
    For now we perform programatic modification and a before/after state.
    """
    def __init__(self, target: QuantumComponent, attribute: str, value):
        super().__init__()
        self.target = target
        self.attribute = attribute
        self.value = value

    def __repr__(self):
        return f"{self.target.full_id()}.{self.attribute} = {str(self.value)}"


class Sweep(Instruction):
    """
    This is a global meta-instruction that performs a sweep over values, effectively
    performing a loop over the instructions replacing a variable with a specific value
    per time.

    Nested sweeps are run in the order they're added and are performed after repeats. So
    a 1000 repeat with a 4 sweep followed by a 2 will run a total of 8000 iterations.
    """
    def __init__(self, operations: Union[SweepValue, List[SweepValue]] = None):
        super().__init__()

        if operations is None:
            operations = []
        elif not isinstance(operations, List):
            operations = [operations]

        self.variables: Dict[str, List[Any]] = {op.name: op.value for op in operations}

        # Get the length of the variables, which we will then assume is the sweep
        # length.
        sweep_lengths = [len(value) for value in self.variables.values()]
        if len(set(sweep_lengths)) > 1:
            raise ValueError("Sweep variables have inconsistent lengths.")

    @property
    def length(self):
        return next(iter([len(value) for value in self.variables.values()]), 0)

    def __repr__(self):
        args = ','.join(key + "=" + str(value) for key, value in self.variables.items())
        return f"sweep {args}"


class Jump(Instruction):
    """
    Classic jump instruction, should be linked to label with an optional condition.
    """
    def __init__(self, label: Union[str, Label], condition=None):
        self.condition = condition
        if isinstance(label, Label):
            self.target = label.name
        else:
            self.target = label

    def __repr__(self):
        if self.condition is not None:
            return f"if {str(self.condition)} -> {str(self.target)}"
        else:
            return f"-> {str(self.target)}"


class BinaryOperator:
    """ Binary operator, such as ``x == y``, ``x != y`` etc."""
    def __init__(self, left, right):
        self.left = left
        self.right = right


class Equals(BinaryOperator):
    def __repr__(self):
        return f"{str(self.left)} == {str(self.right)}"


class NotEquals(BinaryOperator):
    def __repr__(self):
        return f"{str(self.left)} != {str(self.right)}"


class GreaterThan(BinaryOperator):
    def __repr__(self):
        return f"{str(self.left)} > {str(self.right)}"


class GreaterOrEqualThan(BinaryOperator):
    def __repr__(self):
        return f"{str(self.left)} >= {str(self.right)}"


class LessThan(BinaryOperator):
    def __repr__(self):
        return f"{str(self.left)} < {str(self.right)}"


class LessOrEqualThan(BinaryOperator):
    def __repr__(self):
        return f"{str(self.left)} <= {str(self.right)}"


def is_generated_name(name: str):
    return re.match(".*generated_name_[0-9]*$", name) is not None


def build_generated_name(existing_names=None, prefix=None):
    if existing_names is None:
        existing_names = set()

    if prefix is None:
        prefix = ""

    if any(prefix) and not prefix.endswith("_"):
        prefix = f"{prefix}_"

    variable_name = \
        f"{prefix}generated_name_{np.random.randint(np.iinfo(np.int32).max)}"
    while variable_name in existing_names:
        variable_name = \
            f"{prefix}generated_name_{np.random.randint(np.iinfo(np.int32).max)}"

    existing_names.add(variable_name)
    return variable_name


class Variable:
    """
    States that this value is actually a variable that should be fetched instead.
    """
    def __init__(self, name, var_type=None, value=None):
        self.name = name
        self.var_type = var_type
        self.value = value

    @staticmethod
    def with_random_name(existing_names=None, var_type=None, value=None):
        return Variable(Variable.generate_name(existing_names), var_type, value)

    @staticmethod
    def generate_name(existing_names=None):
        return build_generated_name(existing_names)

    def __repr__(self):
        return self.name


class Label(Instruction):
    """
    Label to apply to a line of code. Used as anchors for other instructions like jumps.
    """
    def __init__(self, name):
        """ If you need a name, use generate_name and pass in existing values. """
        self.name = name

    @staticmethod
    def with_random_name(existing_names=None):
        return Label(Label.generate_name(existing_names))

    @staticmethod
    def generate_name(existing_names=None):
        return build_generated_name(existing_names)

    def __repr__(self):
        return f"{self.name}:"


class IndexAccessor(Variable):
    """ Used to access an array index on a particular variable. """
    def __init__(self, name, index):
        super().__init__(name)
        self.index = index

    def __repr__(self):
        return f"{self.name}[{self.index}]"


class ResultsProcessing(Instruction):
    def __init__(self, variable: str, res_processing: InlineResultsProcessing):
        self.variable = variable
        self.results_processing = res_processing

    def __repr__(self):
        return f"{self.variable}: {str(self.results_processing.name)}"
