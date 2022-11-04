# Copyright 2018-2022 Xanadu Quantum Technologies Inc.

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
r"""
This module contains the :class:`~.LightningGPU` class, a PennyLane simulator device that
interfaces with the NVIDIA cuQuantum cuStateVec simulator library for GPU-enabled calculations.
"""
from typing import List, Union
from warnings import warn
from itertools import islice, product

import numpy as np
from concurrent.futures import ThreadPoolExecutor
import concurrent.futures

from pennylane import (
    math,
    QubitDevice,
    BasisState,
    QubitStateVector,
    DeviceError,
    Projector,
    Hermitian,
    Rot,
    QuantumFunctionError,
    QubitStateVector,
)
from pennylane_lightning import LightningQubit
from pennylane.operation import Tensor, Operation
from pennylane.measurements import Expectation, MeasurementProcess, State
from pennylane.wires import Wires

# tolerance for numerical errors
tolerance = 1e-6

# Remove after the next release of PL
# Add from pennylane import matrix
import pennylane as qml

from ._version import __version__

try:
    from .lightning_gpu_qubit_ops import (
        LightningGPU_C128,
        LightningGPU_C64,
        AdjointJacobianGPU_C128,
        AdjointJacobianGPU_C64,
        device_reset,
        is_gpu_supported,
        get_gpu_arch,
        DevPool,
        DevTag,
        NamedObsGPU_C64,
        NamedObsGPU_C128,
        TensorProdObsGPU_C64,
        TensorProdObsGPU_C128,
        HamiltonianGPU_C64,
        HamiltonianGPU_C128,
        SparseHamiltonianGPU_C64,
        SparseHamiltonianGPU_C128,
        OpsStructGPU_C128,
        OpsStructGPU_C64,
    )

    from ._serialize import _serialize_ob, _serialize_observables, _serialize_ops
    from ctypes.util import find_library
    from importlib import util as imp_util

    if find_library("custatevec") == None and not imp_util.find_spec("cuquantum"):
        raise ImportError(
            'cuQuantum libraries not found. Please check your "LD_LIBRARY_PATH" environment variable,'
            'or ensure you have installed the appropriate distributable "cuQuantum" package.'
        )
    if not is_gpu_supported():
        raise ValueError(f"CUDA device is an unsupported version: {get_gpu_arch()}")

    CPP_BINARY_AVAILABLE = True
except (ModuleNotFoundError, ImportError, ValueError) as e:
    warn(str(e), UserWarning)
    CPP_BINARY_AVAILABLE = False


def _gpu_dtype(dtype):
    if dtype not in [np.complex128, np.complex64]:
        raise ValueError(f"Data type is not supported for state-vector computation: {dtype}")
    return LightningGPU_C128 if dtype == np.complex128 else LightningGPU_C64


def _H_dtype(dtype):
    "Utility to choose the appropriate H type based on state-vector precision"
    if dtype not in [np.complex128, np.complex64]:
        raise ValueError(f"Data type is not supported for state-vector computation: {dtype}")
    return HamiltonianGPU_C128 if dtype == np.complex128 else HamiltonianGPU_C64


_name_map = {"PauliX": "X", "PauliY": "Y", "PauliZ": "Z", "Identity": "I"}

allowed_operations = {
    "Identity",
    "BasisState",
    "QubitStateVector",
    "QubitUnitary",
    "ControlledQubitUnitary",
    "MultiControlledX",
    "DiagonalQubitUnitary",
    "PauliX",
    "PauliY",
    "PauliZ",
    "MultiRZ",
    "Hadamard",
    "S",
    "Adjoint(S)",
    "T",
    "Adjoint(T)",
    "SX",
    "Adjoint(SX)",
    "CNOT",
    "SWAP",
    "ISWAP",
    "PSWAP",
    "Adjoint(ISWAP)",
    "SISWAP",
    "Adjoint(SISWAP)",
    "SQISW",
    "CSWAP",
    "Toffoli",
    "CY",
    "CZ",
    "PhaseShift",
    "ControlledPhaseShift",
    "CPhase",
    "RX",
    "RY",
    "RZ",
    "Rot",
    "CRX",
    "CRY",
    "CRZ",
    "CRot",
    "IsingXX",
    "IsingYY",
    "IsingZZ",
    "IsingXY",
    "SingleExcitation",
    "SingleExcitationPlus",
    "SingleExcitationMinus",
    "DoubleExcitation",
    "DoubleExcitationPlus",
    "DoubleExcitationMinus",
    "QubitCarry",
    "QubitSum",
    "OrbitalRotation",
    "QFT",
    "ECR",
}


# class LightningGPU(LightningQubit):
class LightningGPU(QubitDevice):
    """PennyLane-Lightning-GPU device.

    Args:
        wires (int): the number of wires to initialize the device with
        sync (bool): immediately sync with host-sv after applying operations
        c_dtype: Datatypes for statevector representation. Must be one of ``np.complex64`` or ``np.complex128``.
    """

    name = "PennyLane plugin for GPU-backed Lightning device using NVIDIA cuQuantum SDK"
    short_name = "lightning.gpu"
    pennylane_requires = ">=0.22"
    version = __version__
    author = "Xanadu Inc."
    _CPP_BINARY_AVAILABLE = True

    operations = allowed_operations
    observables = {
        "PauliX",
        "PauliY",
        "PauliZ",
        "Hadamard",
        "SparseHamiltonian",
        "Hamiltonian",
        "Identity",
    }

    def __init__(
        self,
        wires,
        *,
        sync=True,
        c_dtype=np.complex128,
        shots=None,
        batch_obs: Union[bool, int] = False,
        analytic=None,
    ):
        if c_dtype is np.complex64:
            r_dtype = np.float32
            self.use_csingle = True
        elif c_dtype is np.complex128:
            r_dtype = np.float64
            self.use_csingle = False
        else:
            raise TypeError(f"Unsupported complex Type: {c_dtype}")
        super().__init__(wires, shots=shots, r_dtype=r_dtype, c_dtype=c_dtype, analytic=analytic)
        self._gpu_state = _gpu_dtype(c_dtype)(self.num_wires)
        self._create_basis_state_GPU(0)
        self._sync = sync
        self._dp = DevPool()
        self._batch_obs = batch_obs

        self._state = self._create_basis_state_default(0)
        self._pre_rotated_state = self._state

    def reset(self):
        super().reset()
        # init the state vector to |00..0>
        self._state = self._create_basis_state_default(0)
        self._pre_rotated_state = self._state
        self._gpu_state.resetGPU(False)  # Sync reset

    def syncH2D(self, use_async=False):
        """Explicitly synchronize CPU data to GPU"""
        self._gpu_state.HostToDevice(self._state.ravel(order="C"), use_async)

    def syncD2H(self, use_async=False):
        """Explicitly synchronize GPU data to CPU"""
        if len(self._state) != 2**self.num_wires:
            state = np.zeros(2**self.num_wires, dtype=np.complex128)
            state = self._asarray(state, dtype=self.C_DTYPE)
            self._state = self._reshape(state, [2] * self.num_wires)

        self._gpu_state.DeviceToHost(self._state.ravel(order="C"), use_async)
        self._pre_rotated_state = self._state

    @property
    def state(self):
        # Flattening the state.
        shape = (1 << self.num_wires,)
        self.syncD2H()
        return self._reshape(self._pre_rotated_state, shape)

    def _get_batch_size(self, tensor, expected_shape, expected_size):
        """Determine whether a tensor has an additional batch dimension for broadcasting,
        compared to an expected_shape."""
        size = self._size(tensor)
        if self._ndim(tensor) > len(expected_shape) or size > expected_size:
            return size // expected_size

        return None

    def _apply_diagonal_unitary(self, state, phases, wires):
        r"""Apply multiplication of a phase vector to subsystems of the quantum state.

        This represents the multiplication with diagonal gates in a more efficient manner.

        Args:
            state (array[complex]): input state
            phases (array): vector to multiply
            wires (Wires): target wires

        Returns:
            array[complex]: output state
        """
        # translate to wire labels used by device
        device_wires = self.map_wires(wires)
        dim = 2 ** len(device_wires)
        batch_size = self._get_batch_size(phases, (dim,), dim)

        # reshape vectors
        shape = [2] * len(device_wires)
        if batch_size is not None:
            shape.insert(0, batch_size)
        phases = self._cast(self._reshape(phases, shape), dtype=self.C_DTYPE)

        state_indices = ABC[: self.num_wires]
        affected_indices = "".join(ABC_ARRAY[list(device_wires)].tolist())

        einsum_indices = f"...{affected_indices},...{state_indices}->...{state_indices}"
        return self._einsum(einsum_indices, phases, state)

    def _apply_unitary_einsum(self, state, mat, wires):
        r"""Apply multiplication of a matrix to subsystems of the quantum state.

        This function uses einsum instead of tensordot. This approach is only
        faster for single- and two-qubit gates.

        Args:
            state (array[complex]): input state
            mat (array): matrix to multiply
            wires (Wires): target wires

        Returns:
            array[complex]: output state
        """
        # translate to wire labels used by device
        device_wires = self.map_wires(wires)

        dim = 2 ** len(device_wires)
        batch_size = self._get_batch_size(mat, (dim, dim), dim**2)

        # If the matrix is broadcasted, it is reshaped to have leading axis of size mat_batch_size
        shape = [2] * (len(device_wires) * 2)
        if batch_size is not None:
            shape.insert(0, batch_size)
        mat = self._cast(self._reshape(mat, shape), dtype=self.C_DTYPE)

        # Tensor indices of the quantum state
        state_indices = ABC[: self.num_wires]

        # Indices of the quantum state affected by this operation
        affected_indices = "".join(ABC_ARRAY[list(device_wires)].tolist())

        # All affected indices will be summed over, so we need the same number of new indices
        new_indices = ABC[self.num_wires : self.num_wires + len(device_wires)]

        # The new indices of the state are given by the old ones with the affected indices
        # replaced by the new_indices
        new_state_indices = functools.reduce(
            lambda old_string, idx_pair: old_string.replace(idx_pair[0], idx_pair[1]),
            zip(affected_indices, new_indices),
            state_indices,
        )

        # We now put together the indices in the notation numpy's einsum requires
        # This notation allows for the state, the matrix, or both to be broadcasted
        einsum_indices = (
            f"...{new_indices}{affected_indices},...{state_indices}->...{new_state_indices}"
        )

        return self._einsum(einsum_indices, mat, state)

    def _create_basis_state_default(self, index):
        """Return a computational basis state over all wires.
        Args:
            index (int): integer representing the computational basis state
        Returns:
            array[complex]: complex array of shape ``[2]*self.num_wires``
            representing the statevector of the basis state
        Note: This function does not support broadcasted inputs yet.
        """

        state = np.zeros(2, dtype=np.complex128)
        state[index] = 1
        state = self._asarray(state, dtype=self.C_DTYPE)
        return self._reshape(state, [2])
        """
        state = np.zeros(2**self.num_wires, dtype=np.complex128)
        state[index] = 1
        state = self._asarray(state, dtype=self.C_DTYPE)
        self._state = self._reshape(state, [2] * self.num_wires)
        return self._reshape(state, [2] * self.num_wires)
        """

    def _create_basis_state_GPU(self, index, use_async=False):
        self._gpu_state.setBasisState(index, use_async)

    def _apply_state_vector_GPU(self, state, device_wires, use_async=False):
        # Initialize the internal state vector in a specified state.
        # Args:
        #    state (array[complex]): normalized input state of length ``2**len(wires)``
        #        or broadcasted state of shape ``(batch_size, 2**len(wires))``
        #    device_wires (Wires): wires that get initialized in the state

        # translate to wire labels used by device
        device_wires = self.map_wires(device_wires)
        dim = 2 ** len(device_wires)

        state = self._asarray(state, dtype=self.C_DTYPE)
        batch_size = self._get_batch_size(state, (dim,), dim)
        output_shape = [2] * self.num_wires

        if batch_size is not None:
            output_shape.insert(0, batch_size)

        if not (state.shape in [(dim,), (batch_size, dim)]):
            raise ValueError("State vector must have shape (2**wires,) or (batch_size, 2**wires).")

        if not qml.math.is_abstract(state):
            norm = qml.math.linalg.norm(state, axis=-1, ord=2)
            if not qml.math.allclose(norm, 1.0, atol=tolerance):
                raise ValueError("Sum of amplitudes-squared does not equal one.")

        if len(device_wires) == self.num_wires and Wires(sorted(device_wires)) == device_wires:
            # Initialize the entire device state with the input state
            self._state = self._reshape(state, output_shape)
            self.syncH2D()
            return

        # generate basis states on subset of qubits via the cartesian product
        basis_states = np.array(list(product([0, 1], repeat=len(device_wires))))

        # get basis states to alter on full set of qubits
        unravelled_indices = np.zeros((2 ** len(device_wires), self.num_wires), dtype=int)
        unravelled_indices[:, device_wires] = basis_states

        # get indices for which the state is changed to input state vector elements
        ravelled_indices = np.ravel_multi_index(unravelled_indices.T, [2] * self.num_wires)

        # state = self._scatter(ravelled_indices, state, [2**self.num_wires])
        self._gpu_state.setStateVector(ravelled_indices, state, use_async)

    def _apply_basis_state_GPU(self, state, wires):
        # Initialize the state vector in a specified computational basis state.
        # Args:
        #    state (array[int]): computational basis state of shape ``(wires,)``
        #        consisting of 0s and 1s.
        #    wires (Wires): wires that the provided computational state should be initialized on
        # Note: This function does not support broadcasted inputs yet.

        # translate to wire labels used by device
        device_wires = self.map_wires(wires)

        # length of basis state parameter
        n_basis_state = len(state)

        if not set(state.tolist()).issubset({0, 1}):
            raise ValueError("BasisState parameter must consist of 0 or 1 integers.")

        if n_basis_state != len(device_wires):
            raise ValueError("BasisState parameter and wires must be of equal length.")

        # get computational basis state number
        basis_states = 2 ** (self.num_wires - 1 - np.array(device_wires))
        basis_states = qml.math.convert_like(basis_states, state)
        num = int(qml.math.dot(state, basis_states))

        self._gpu_state.setZeroState(0, False)
        self._create_basis_state_GPU(num)

    # To be able to validate the adjoint method [_validate_adjoint_method(device)],
    #  the qnode requires the definition of:
    # ["_apply_operation", "_apply_unitary", "adjoint_jacobian"]
    def _apply_operation():
        pass

    def _apply_unitary():
        pass

    @classmethod
    def capabilities(cls):
        capabilities = super().capabilities().copy()
        capabilities.update(
            model="qubit",
            supports_inverse_operations=True,
            supports_analytic_computation=True,
            supports_finite_shots=True,
            returns_state=True,
        )
        capabilities.pop("passthru_devices", None)
        return capabilities

    def statistics(self, observables, shot_range=None, bin_size=None, circuit=None):
        ## Ensure D2H sync before calculating non-GPU supported operations
        if self._sync:
            self.syncD2H()
        return super().statistics(observables, shot_range, bin_size, circuit)

    def apply_cq(self, operations, **kwargs):
        # Skip over identity operations instead of performing
        # matrix multiplication with the identity.
        skipped_ops = ["Identity"]

        for o in operations:
            if o.base_name in skipped_ops:
                continue
            name = o.name.split(".")[0]  # The split is because inverse gates have .inv appended
            method = getattr(self._gpu_state, name, None)

            wires = self.wires.indices(o.wires)

            if method is None:
                # Inverse can be set to False since qml.matrix(o) is already in inverted form
                try:
                    mat = qml.matrix(o)
                except AttributeError:  # pragma: no cover
                    # To support older versions of PL
                    mat = o.matrix

                if len(mat) == 0:
                    raise Exception("Unsupported operation")
                self._gpu_state.apply(
                    name,
                    wires,
                    False,
                    [],
                    mat.ravel(order="C"),  # inv = False: Matrix already in correct form;
                )  # Parameters can be ignored for explicit matrices; F-order for cuQuantum

            else:
                inv = o.inverse
                param = o.parameters
                method(wires, inv, param)

    def apply(self, operations, **kwargs):
        # State preparation is currently done in Python
        if operations:  # make sure operations[0] exists
            if isinstance(operations[0], QubitStateVector):
                self._apply_state_vector_GPU(
                    operations[0].parameters[0].copy(), operations[0].wires
                )
                del operations[0]
            elif isinstance(operations[0], BasisState):
                self._apply_basis_state_GPU(operations[0].parameters[0], operations[0].wires)
                del operations[0]

        for operation in operations:
            if isinstance(operation, (QubitStateVector, BasisState)):
                raise DeviceError(
                    "Operation {} cannot be used after other Operations have already been "
                    "applied on a {} device.".format(operation.name, self.short_name)
                )

        self.apply_cq(operations)
        if self._sync:
            self.syncD2H()

    @staticmethod
    def _check_adjdiff_supported_measurements(measurements: List[MeasurementProcess]):
        """Check whether given list of measurement is supported by adjoint_diff.
        Args:
            measurements (List[MeasurementProcess]): a list of measurement processes to check.
        Returns:
            Expectation or State: a common return type of measurements.
        """
        if len(measurements) == 0:
            return None

        if len(measurements) == 1 and measurements[0].return_type is State:
            # return State
            raise QuantumFunctionError("Not supported")

        # The return_type of measurement processes must be expectation
        if not all([m.return_type is Expectation for m in measurements]):
            raise QuantumFunctionError(
                "Adjoint differentiation method does not support expectation return type "
                "mixed with other return types"
            )

        for m in measurements:
            if not isinstance(m.obs, Tensor):
                if isinstance(m.obs, Projector):
                    raise QuantumFunctionError(
                        "Adjoint differentiation method does not support the Projector observable"
                    )
                if isinstance(m.obs, Hermitian):
                    raise QuantumFunctionError(
                        "LightningGPU adjoint differentiation method does not currently support the Hermitian observable"
                    )
            else:
                if any([isinstance(o, Projector) for o in m.obs.non_identity_obs]):
                    raise QuantumFunctionError(
                        "Adjoint differentiation method does not support the Projector observable"
                    )
                if any([isinstance(o, Hermitian) for o in m.obs.non_identity_obs]):
                    raise QuantumFunctionError(
                        "LightningGPU adjoint differentiation method does not currently support the Hermitian observable"
                    )
        return Expectation

    @staticmethod
    def _check_adjdiff_supported_operations(operations):
        """Check Lightning adjoint differentiation method support for a tape.

        Raise ``QuantumFunctionError`` if ``tape`` contains not supported measurements,
        observables, or operations by the Lightning adjoint differentiation method.

        Args:
            tape (.QuantumTape): quantum tape to differentiate.
        """
        for op in operations:
            if op.num_params > 1 and not isinstance(op, Rot):
                raise QuantumFunctionError(
                    f"The {op.name} operation is not supported using "
                    'the "adjoint" differentiation method'
                )

    def adjoint_jacobian(self, tape, starting_state=None, use_device_state=False, **kwargs):
        if self.shots is not None:
            warn(
                "Requested adjoint differentiation to be computed with finite shots."
                " The derivative is always exact when using the adjoint differentiation method.",
                UserWarning,
            )

        tape_return_type = self._check_adjdiff_supported_measurements(tape.measurements)

        if len(tape.trainable_params) == 0:
            return np.array(0)

        # Check adjoint diff support
        self._check_adjdiff_supported_operations(tape.operations)

        # Initialization of state
        if starting_state is not None:
            ket = np.ravel(starting_state, order="C")
        else:
            if not use_device_state:
                self.reset()
                self.execute(tape)
            ket = np.ravel(self._pre_rotated_state, order="C")

        if self.use_csingle:
            adj = AdjointJacobianGPU_C64()
            ket = ket.astype(np.complex64)
        else:
            adj = AdjointJacobianGPU_C128()

        obs_serialized, obs_offsets = _serialize_observables(
            tape, self.wire_map, use_csingle=self.use_csingle
        )
        ops_serialized, use_sp = _serialize_ops(tape, self.wire_map, use_csingle=self.use_csingle)
        ops_serialized = adj.create_ops_list(*ops_serialized)

        trainable_params = sorted(tape.trainable_params)

        tp_shift = []
        record_tp_rows = []
        all_params = 0

        for op_idx, tp in enumerate(trainable_params):
            op, _ = tape.get_operation(
                op_idx
            )  # get op_idx-th operator among differentiable operators

            if isinstance(op, Operation) and not isinstance(op, (BasisState, QubitStateVector)):
                # We now just ignore non-op or state preps
                tp_shift.append(tp)
                record_tp_rows.append(all_params)
            all_params += 1

        if use_sp:
            # When the first element of the tape is state preparation. Still, I am not sure
            # whether there must be only one state preparation...
            tp_shift = [i - 1 for i in tp_shift]

        """
        This path enables controlled batching over the requested observables, be they explicit, or part of a Hamiltonian.
        The traditional path will assume there exists enough free memory to preallocate all arrays and run through each observable iteratively.
        However, for larger system, this becomes impossible, and we hit memory issues very quickly. the batching support here enables several functionalities:
        - Pre-allocate memory for all observables on the primary GPU (`batch_obs=False`, default behaviour): This is the simplest path, and works best for few observables, and moderate qubit sizes. All memory is preallocated for each observable, and run through iteratively on a single GPU.
        - Evenly distribute the observables over all available GPUs (`batch_obs=True`): This will evenly split the data into ceil(num_obs/num_gpus) chunks, and allocate enough space on each GPU up-front before running through them concurrently. This relies on C++ threads to handle the orchestration.
        - Allocate at most `n` observables per GPU (`batch_obs=n`): Providing an integer value restricts each available GPU to at most `n` copies of the statevector, and hence `n` given observables for a given batch. This will iterate over the data in chnuks of size `n*num_gpus`.
        """

        if self._batch_obs:
            num_obs = len(obs_serialized)
            batch_size = (
                num_obs
                if isinstance(self._batch_obs, bool)
                else self._batch_obs * self._dp.getTotalDevices()
            )
            jac = []
            for chunk in range(0, num_obs, batch_size):
                obs_chunk = obs_serialized[chunk : chunk + batch_size]
                jac_chunk = adj.adjoint_jacobian_batched(
                    self._gpu_state,
                    obs_chunk,
                    ops_serialized,
                    tp_shift,
                )
                jac.extend(jac_chunk)
        else:
            jac = adj.adjoint_jacobian(
                self._gpu_state,
                obs_serialized,
                ops_serialized,
                tp_shift,
            )

        jac = np.array(jac)  # only for parameters differentiable with the adjoint method
        jac = jac.reshape(-1, len(tp_shift))
        jac_r = np.zeros((len(tape.observables), all_params))

        # Reduce over decomposed expval(H), if required.
        for idx in range(len(obs_offsets[0:-1])):
            if (obs_offsets[idx + 1] - obs_offsets[idx]) > 1:
                jac_r[idx, :] = np.sum(jac[obs_offsets[idx] : obs_offsets[idx + 1], :], axis=0)
            else:
                jac_r[idx, :] = jac[obs_offsets[idx] : obs_offsets[idx + 1], :]

        return jac_r

    def vjp(self, measurements, dy, starting_state=None, use_device_state=False):
        """Generate the processing function required to compute the vector-Jacobian products of a tape."""
        if self.shots is not None:
            warn(
                "Requested adjoint differentiation to be computed with finite shots."
                " The derivative is always exact when using the adjoint differentiation method.",
                UserWarning,
            )

        tape_return_type = self._check_adjdiff_supported_measurements(measurements)

        if math.allclose(dy, 0) or tape_return_type is None:
            return lambda tape: math.convert_like(np.zeros(len(tape.trainable_params)), dy)

        if tape_return_type is Expectation:
            if len(dy) != len(measurements):
                raise ValueError(
                    "Number of observables in the tape must be the same as the length of dy in the vjp method"
                )

            if np.iscomplexobj(dy):
                raise ValueError(
                    "The vjp method only works with a real-valued dy when the tape is returning an expectation value"
                )

            ham = qml.Hamiltonian(dy, [m.obs for m in measurements])

            def processing_fn(tape):
                nonlocal ham
                num_params = len(tape.trainable_params)

                if num_params == 0:
                    return np.array([], dtype=self._state.dtype)

                new_tape = tape.copy()
                new_tape._measurements = [qml.expval(ham)]

                return self.adjoint_jacobian(new_tape, starting_state, use_device_state).reshape(-1)

            return processing_fn

    def sample(self, observable, shot_range=None, bin_size=None, counts=False):
        if observable.name != "PauliZ":
            self.apply_cq(observable.diagonalizing_gates())
            self._samples = self.generate_samples()
        return super().sample(observable, shot_range=shot_range, bin_size=bin_size, counts=counts)

    def expval(self, observable, shot_range=None, bin_size=None):
        if observable.name in [
            "Projector",
            "Hermitian",
        ]:
            self.syncD2H()
            return super().expval(observable, shot_range=shot_range, bin_size=bin_size)

        if self.shots is not None:
            # estimate the expectation value
            samples = self.sample(observable, shot_range=shot_range, bin_size=bin_size)
            return np.squeeze(np.mean(samples, axis=0))

        if observable.name in ["SparseHamiltonian"]:
            CSR_SparseHamiltonian = observable.sparse_matrix().tocsr()
            return self._gpu_state.ExpectationValue(
                CSR_SparseHamiltonian.indptr,
                CSR_SparseHamiltonian.indices,
                CSR_SparseHamiltonian.data,
            )

        if observable.name in ["Hamiltonian"]:
            device_wires = self.map_wires(observable.wires)
            # 16 bytes * (2^13)^2 -> 1GB Hamiltonian limit for GPU transfer before
            if len(device_wires) > 13:
                coeffs = observable.coeffs
                pauli_words = []
                word_wires = []
                for word in observable.ops:
                    compressed_word = []
                    if isinstance(word.name, list):
                        for char in word.name:
                            compressed_word.append(_name_map[char])
                    else:
                        compressed_word.append(_name_map[word.name])
                    word_wires.append(word.wires.tolist())
                    pauli_words.append("".join(compressed_word))
                return self._gpu_state.ExpectationValue(pauli_words, word_wires, coeffs)

            else:
                return self._gpu_state.ExpectationValue(
                    device_wires, qml.matrix(observable).ravel(order="C")
                )

        par = (
            observable.parameters
            if (
                len(observable.parameters) > 0 and isinstance(observable.parameters[0], np.floating)
            )
            else []
        )
        return self._gpu_state.ExpectationValue(
            observable.name,
            self.wires.indices(observable.wires),
            par,  # observables should not pass parameters, use matrix instead
            qml.matrix(observable).ravel(order="C"),
        )

    def probability(self, wires=None, shot_range=None, bin_size=None):
        if self.shots is not None:
            return self.estimate_probability(wires=wires, shot_range=shot_range, bin_size=bin_size)

        wires = wires or self.wires
        wires = Wires(wires)

        # translate to wire labels used by device
        device_wires = self.map_wires(wires)
        # Device returns as col-major orderings, so perform transpose on data for bit-index shuffle for now.
        return (
            self._gpu_state.Probability(device_wires)
            .reshape([2] * len(wires))
            .transpose()
            .reshape(-1)
        )

    def generate_samples(self):
        """Generate samples

        Returns:
            array[int]: array of samples in binary representation with shape ``(dev.shots, dev.num_wires)``
        """
        return self._gpu_state.GenerateSamples(len(self.wires), self.shots).astype(int)

    def var(self, observable, shot_range=None, bin_size=None):
        if self.shots is not None:
            # estimate the var
            # Lightning doesn't support sampling yet
            samples = self.sample(observable, shot_range=shot_range, bin_size=bin_size)
            return np.squeeze(np.var(samples, axis=0))

        adjoint_matrix = math.T(math.conj(qml.matrix(observable)))
        sqr_matrix = np.matmul(adjoint_matrix, qml.matrix(observable))

        mean = self._gpu_state.ExpectationValue(
            [i + "_var" for i in observable.name],
            self.wires.indices(observable.wires),
            observable.parameters,
            qml.matrix(observable).ravel(order="C"),
        )

        squared_mean = self._gpu_state.ExpectationValue(
            [i + "_sqr" for i in observable.name],
            self.wires.indices(observable.wires),
            observable.parameters,
            sqr_matrix.ravel(order="C"),
        )

        return squared_mean - (mean**2)


if not CPP_BINARY_AVAILABLE:

    class LightningGPU(LightningQubit):
        name = "PennyLane plugin for GPU-backed Lightning device using NVIDIA cuQuantum SDK: Lightning CPU fall-back"
        short_name = "lightning.gpu"
        pennylane_requires = ">=0.22"
        version = __version__
        author = "Xanadu Inc."
        _CPP_BINARY_AVAILABLE = False

        def __init__(self, wires, *, c_dtype=np.complex128, **kwargs):
            w_msg = """
            !!!#####################################################################################
            !!!
            !!! WARNING: INSUFFICIENT SUPPORT DETECTED FOR GPU DEVICE WITH `lightning.gpu`
            !!!          DEFAULTING TO CPU DEVICE `lightning.qubit`
            !!!
            !!!#####################################################################################
            """
            warn(
                w_msg,
                RuntimeWarning,
            )
            super().__init__(wires, c_dtype=c_dtype, **kwargs)
