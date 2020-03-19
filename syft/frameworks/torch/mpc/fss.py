"""
This is an implementation of Function Secret Sharing

Useful papers are:
- Function Secret Sharing- Improvements and Extensions, Boyle 2017 https://eprint.iacr.org/2018/707.pdf
- Secure Computation with Preprocessing via Function Secret Sharing, Boyle 2019 https://eprint.iacr.org/2019/1095

Note that the protocols are quite different in aspect from those papers
"""
import hashlib

import torch as th
import syft as sy
from syft.exceptions import EmptyCryptoPrimitiveStoreError
from syft.execution.plan import func2plan
from syft.generic.frameworks.hook.trace import tracer
from syft.workers.base import BaseWorker


λ = 110  # 6  # 63  # security parameter
n = 32  # 8  # 32  # bit precision

no_wrap = {"no_wrap": True}


def initialize_crypto_plans(worker):
    """
    This is called manually for the moment, to build the plan used to perform
    Function Secret Sharing on a specific worker.
    """
    eq_plan_1 = sy.Plan(
        forward_func=lambda x, y: mask_builder(x, y, "eq"),
        owner=worker,
        tags=["#fss_eq_plan_1"],
        is_built=True,
    )
    worker.register_obj(eq_plan_1)
    eq_plan_2 = sy.Plan(
        forward_func=eq_eval_plan, owner=worker, tags=["#fss_eq_plan_2"], is_built=True
    )
    worker.register_obj(eq_plan_2)

    comp_plan_1 = sy.Plan(
        forward_func=lambda x, y: mask_builder(x, y, "comp"),
        owner=worker,
        tags=["#fss_comp_plan_1"],
        is_built=True,
    )
    worker.register_obj(comp_plan_1)
    comp_plan_2 = sy.Plan(
        forward_func=comp_eval_plan, owner=worker, tags=["#fss_comp_plan_2"], is_built=True
    )
    worker.register_obj(comp_plan_2)

    xor_add_plan = sy.Plan(
        forward_func=xor_add_convert_1, owner=worker, tags=["#xor_add_1"], is_built=True
    )
    worker.register_obj(xor_add_plan)
    xor_add_plan = sy.Plan(
        forward_func=xor_add_convert_2, owner=worker, tags=["#xor_add_2"], is_built=True
    )
    worker.register_obj(xor_add_plan)


def request_run_plan(worker, plan_tag, location, return_value, args=tuple(), kwargs=dict()):
    response_ids = [sy.ID_PROVIDER.pop()]
    args = [args, response_ids]

    command = ("run", plan_tag, args, kwargs)

    response = worker.send_command(
        message=command, recipient=location, return_ids=response_ids, return_value=return_value
    )
    return response


def fss_op(x1, x2, type_op="eq"):
    """
    Define the workflow for a binary operation using Function Secret Sharing

    Currently supported operand are = & <=, respectively corresponding to
    type_op = 'eq' and 'comp'

    Args:
        x1: first AST
        x2: second AST
        type_op: type of operation to perform, should be 'eq' or 'comp'

    Returns:
        shares of the comparison
    """

    me = sy.local_worker
    locations = x1.locations

    shares = []
    for location in locations:
        args = (x1.child[location.id], x2.child[location.id])
        share = request_run_plan(
            me, f"#fss_{type_op}_plan_1", location, return_value=True, args=args
        )
        shares.append(share)

    mask_value = sum(shares) % 2 ** n

    shares = []
    for i, location in enumerate(locations):
        args = (th.IntTensor([i]), mask_value)
        share = request_run_plan(
            me, f"#fss_{type_op}_plan_2", location, return_value=False, args=args
        )
        shares.append(share)

    if type_op == "comp":
        prev_shares = shares
        shares = []
        for prev_share, location in zip(prev_shares, locations):
            share = request_run_plan(
                me, f"#xor_add_1", location, return_value=True, args=(prev_share,)
            )
            shares.append(share)

        masked_value = shares[0] ^ shares[1]  # TODO case >2 workers ?

        shares = {}
        for i, prev_share, location in zip(range(len(locations)), prev_shares, locations):
            share = request_run_plan(
                me,
                f"#xor_add_2",
                location,
                return_value=False,
                args=(th.IntTensor([i]), masked_value),
            )
            shares[location.id] = share
    else:
        shares = {loc.id: share for loc, share in zip(locations, shares)}

    response = sy.AdditiveSharingTensor(shares, **x1.get_class_attributes())
    return response


def get_keys(worker, type_op, remove=True):
    """
    Return FSS keys primitives

    Args:
        worker: worker which is doing the computation and has the crypto primitives
        remove: if true, pop out the primitive. If false, only read it. Read mode is
            needed because we're working on virtual workers and they need to gather
            a some point and then re-access the keys.
    """
    primitive_stack = {
        "eq": worker.crypto_store.fss_eq,
        "comp": worker.crypto_store.fss_comp,
        "xor_add": worker.crypto_store.xor_add_couple,
    }[type_op]

    try:
        if remove:
            return primitive_stack.pop(0)
        else:
            return primitive_stack[0]
    except IndexError:
        raise EmptyCryptoPrimitiveStoreError(worker.crypto_store, f"fss_{type_op}")


# share level
def mask_builder(x1, x2, type_op):
    x = x1 - x2
    # Keep the primitive in store as we use it after
    alpha, s_0, *CW = get_keys(x1.owner, type_op, remove=False)
    return x + alpha


# share level
def eq_eval_plan(b, x_masked):
    alpha, s_0, *CW = get_keys(x_masked.owner, type_op="eq", remove=True)
    result_share = DPF.eval(b, x_masked, s_0, *CW)
    return result_share


# share level
def comp_eval_plan(b, x_masked):
    alpha, s_0, *CW = get_keys(x_masked.owner, type_op="comp", remove=True)
    result_share = DIF.eval(b, x_masked, s_0, *CW)
    return result_share


def xor_add_convert_1(x):
    xor_share, add_share = get_keys(x.owner, type_op="xor_add", remove=False)
    return x ^ xor_share


def xor_add_convert_2(b, x):
    xor_share, add_share = get_keys(x.owner, type_op="xor_add", remove=True)
    return add_share * (1 - 2 * x) + x * b


def eq(x1, x2):
    return fss_op(x1, x2, "eq")


def le(x1, x2):
    return fss_op(x1, x2, "comp")


class DPF:
    """Distributed Point Function - used for equality"""

    def __init__(self):
        pass

    @staticmethod
    def keygen():
        beta = th.tensor([1], dtype=th.int32)
        (alpha,) = th.randint(0, 2 ** n, (1,))

        α = bit_decomposition(alpha)
        s, t, CW = Array(n + 1, 2, λ), Array(n + 1, 2), Array(n, 2 * (λ + 1))
        s[0] = randbit(size=(2, λ))
        t[0] = th.tensor([0, 1], dtype=th.uint8)
        for i in range(0, n):
            g0 = G(s[i, 0])
            g1 = G(s[i, 1])
            # Re-use useless randomness
            sL_0, _, sR_0, _ = split(g0, [λ, 1, λ, 1])
            sL_1, _, sR_1, _ = split(g1, [λ, 1, λ, 1])
            s_rand = (sL_0 ^ sL_1) * α[i] + (sR_0 ^ sR_1) * (1 - α[i])

            cw_i = TruthTableDPF(s_rand, α[i])
            CW[i] = cw_i ^ g0 ^ g1

            for b in (0, 1):
                τ = [g0, g1][b] ^ (t[i, b] * CW[i])
                τ = τ.reshape(2, λ + 1)
                s[i + 1, b], t[i + 1, b] = split(τ[α[i]], [λ, 1])

        CW_n = (-1) ** t[n, 1] * (beta.to(th.uint8) - Convert(s[n, 0]) + Convert(s[n, 1]))

        return (alpha,) + s[0].unbind() + (CW, CW_n)

    @staticmethod
    def eval(b, x, *k_b):
        x = bit_decomposition(x)
        s, t = Array(n + 1, λ), Array(n + 1, 1)
        s[0] = k_b[0]
        # here k[1:] is (CW, CW_n)
        CW = k_b[1].unbind() + (k_b[2],)
        t[0] = b
        for i in range(0, n):
            τ = G(s[i]) ^ (t[i] * CW[i])
            τ = τ.reshape(2, λ + 1)
            s[i + 1], t[i + 1] = split(τ[x[i]], [λ, 1])
        return (-1) ** b * (Convert(s[n]) + t[n] * CW[n])


class DIF:
    "Distributed Interval Function - used for comparison <="

    def __init__(self):
        pass

    @staticmethod
    def keygen():
        (alpha,) = th.randint(0, 2 ** n, (1,))
        α = bit_decomposition(alpha)
        s, t, CW = Array(n + 1, 2, λ), Array(n + 1, 2), Array(n, 2 + 2 * (λ + 1))
        s[0] = randbit(size=(2, λ))
        t[0] = th.tensor([0, 1], dtype=th.uint8)
        for i in range(0, n):
            h0 = H(s[i, 0])
            h1 = H(s[i, 1])
            # Re-use useless randomness
            _, _, sL_0, _, sR_0, _ = split(h0, [1, 1, λ, 1, λ, 1])
            _, _, sL_1, _, sR_1, _ = split(h1, [1, 1, λ, 1, λ, 1])
            s_rand = (sL_0 ^ sL_1) * α[i] + (sR_0 ^ sR_1) * (1 - α[i])
            cw_i = TruthTableDIF(s_rand, α[i])
            CW[i] = cw_i ^ h0 ^ h1

            for b in (0, 1):
                τ = [h0, h1][b] ^ (t[i, b] * CW[i])
                τ = τ.reshape(2, λ + 2)
                σ_leaf, s[i + 1, b], t[i + 1, b] = split(τ[α[i]], [1, λ, 1])

        return (alpha,) + s[0].unbind() + (CW,)

    @staticmethod
    def eval(b, x, *k_b):
        FnOutput = Array(n + 1, 1)
        x = bit_decomposition(x)
        s, t = Array(n + 1, λ), Array(n + 1, 1)
        s[0] = k_b[0]
        CW = k_b[1].unbind()
        t[0] = b
        for i in range(0, n):
            τ = H(s[i]) ^ (t[i] * CW[i])
            τ = τ.reshape(2, λ + 2)
            σ_leaf, s[i + 1], t[i + 1] = split(τ[x[i]], [1, λ, 1])
            FnOutput[i] = σ_leaf

        # Last tour, the other σ is also a leaf:
        FnOutput[n] = t[n]
        return FnOutput.sum() % 2


# PRG
def G(seed):
    assert len(seed) == λ
    enc_str = str(seed.tolist()).encode()
    h = hashlib.sha3_256(enc_str)
    r = h.digest()
    binary_str = bin(int.from_bytes(r, byteorder="big"))[2 : 2 + (2 * (λ + 1))]
    return th.tensor(list(map(int, binary_str)), dtype=th.uint8)


def H(seed):
    assert len(seed) == λ
    enc_str = str(seed.tolist()).encode()
    h = hashlib.sha3_256(enc_str)
    r = h.digest()
    binary_str = bin(int.from_bytes(r, byteorder="big"))[2 : 2 + 2 + (2 * (λ + 1))]
    return th.tensor(list(map(int, binary_str)), dtype=th.uint8)


# bit_pow_lambda = th.flip(2 ** th.arange(λ), (0,)).to(th.uint8)
def Convert(bits):
    bit_pow_lambda = th.flip(2 ** th.arange(λ), (0,)).to(th.uint8)
    return bits.dot(bit_pow_lambda)


def Array(*shape):
    return th.empty(shape, dtype=th.uint8)


# bit_pow_n = th.flip(2 ** th.arange(n), (0,))
def bit_decomposition(x):
    bit_pow_n = th.flip(2 ** th.arange(n), (0,))
    return ((x & bit_pow_n) > 0).to(th.int8)


def randbit(size):
    return th.randint(2, size=size)


def concat(*args, **kwargs):
    return th.cat(args, **kwargs)


def split(x, idx):
    return th.split(x, idx)


# one = th.tensor([1], dtype=th.uint8)
def TruthTableDPF(s, α_i):
    one = th.tensor([1], dtype=th.uint8)
    Table = th.zeros((2, λ + 1), dtype=th.uint8)
    Table[α_i] = concat(s, one)
    return Table.flatten()


def TruthTableDIF(s, α_i):
    leafTable = th.zeros((2, 1), dtype=th.uint8)
    # if α_i is 0, then ending on the leaf branch means your bit is 1 to you're > α so you should get 0
    # if α_i is 1, then ending on the leaf branch means your bit is 0 to you're < α so you should get 1
    leaf_value = α_i
    leafTable[1 - α_i] = leaf_value

    nextTable = th.zeros((2, λ + 1), dtype=th.uint8)
    one = th.tensor([1], dtype=th.uint8)
    nextTable[α_i] = concat(s, one)

    return concat(leafTable, nextTable, axis=1).flatten()