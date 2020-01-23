from numpyro.optim import _NumpyroOptim, _add_doc
from jax.experimental.optimizers import optimizer, make_schedule
import jax.numpy as np
from jax import tree_map, tree_multimap, tree_leaves

from .svi import full_norm

def adadp(
        step_size=1e-3,
        tol=1.0,
        stability_check=True,
        alpha_min=0.9,
        alpha_max=1.1
    ):
    """Construct optimizer triple for the adaptive learning rate optimizer of
    Koskela and Honkela.

    Reference:
    A. Koskela, A. Honkela: Learning Rate Adaptation for Federated and
    Differentially Private Learning (https://arxiv.org/abs/1809.03832).

    Args:
    step_size: the initial step size
    tol: error tolerance for the discretized gradient steps
    stability_check: settings to True rejects some updates in favor of a more
        stable algorithm
    alpha_min: lower multiplitcative bound of learning rate update per step
    alpha_max: upper multiplitcative bound of learning rate update per step

    Returns:
        An (init_fun, update_fun, get_params) triple.
    """
    step_size = make_schedule(step_size)
    def init(x0):
        lr = step_size(0)
        x_stepped = tree_map(lambda n: np.zeros_like(n), x0)
        return x0, lr, x_stepped, x0

    def update(i, g, state):
        x, lr, x_stepped, x_prev = state

        def compute_update_step(x, g, step_size_):
            return tree_multimap(lambda x_, g_: x_ - step_size_ * g_, x, g)

        new_x = compute_update_step(x, g, 0.5 * lr)
        if i % 2 == 0:
            x_prev = x
            x_stepped = compute_update_step(x, g, lr)
            return new_x, lr, x_stepped, x_prev
        else:
            x_stepped_parts = tree_leaves(x_stepped)
            new_x_parts = tree_leaves(new_x)

            err_e = [
                np.sum(((x_full - x_halfs)/np.maximum(1., x_full)) ** 2)
                for x_full, x_halfs in zip(x_stepped_parts, new_x_parts)
            ]
            # note(lumip): paper specifies the approximate error function as
            #   using absolute values, but since we square anyways, those are
            #   not required here; the resulting array is partial squared sums
            #   of the l2-norm over all gradient elements (per gradient site)

            err_e = np.sqrt(np.sum(err_e)) # summing partial gradient norm
            
            new_lr = lr * np.minimum(
                np.maximum(np.sqrt(tol/err_e), 0.9), 1.1
            )

            if stability_check and err_e > tol:
                new_x = x_prev
            return new_x, new_lr, x_stepped, x_prev

    def get_params(state):
        x = state[0]
        return x
    return init, update, get_params

@_add_doc(adadp)
class ADADP(_NumpyroOptim):

    def __init__(self,
                 step_size=1e-3,
                 tol=1.0,
                 stability_check=True,
                 alpha_min=0.9,
                 alpha_max=1.1) -> None:

        super(ADADP, self).__init__(
            adadp, step_size, tol, stability_check, alpha_min, alpha_max
        )
