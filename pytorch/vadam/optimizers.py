import math
import torch
from torch.optim.optimizer import Optimizer

#################################
## PyTorch Optimizer for Vadam ##
#################################

class Vadam(Optimizer):
    """Implements Vadam algorithm.
    It has been proposed in `Adam: A Method for Stochastic Optimization`_.
    Arguments:
        params (iterable): iterable of parameters to optimize or dicts defining
            parameter groups
        train_set_size (int): number of data points in the full training set 
            (objective assumed to be on the form (1/M)*sum(-log p))
        lr (float, optional): learning rate (default: 1e-3)
        betas (Tuple[float, float], optional): coefficients used for computing
            running averages of gradient and its square (default: (0.9, 0.999))
        prior_prec (float, optional): prior precision on parameters
            (default: 1.0)
        prec_init (float, optional): initial precision for variational dist. q
            (default: 1.0)
        num_samples (float, optional): number of MC samples
            (default: 1)
    """

    def __init__(self, params, train_set_size, lr=1e-3, betas=(0.9, 0.999), prior_prec=1.0, prec_init=1.0, num_samples=1):
        if not 0.0 <= lr:
            raise ValueError("Invalid learning rate: {}".format(lr))
        if not 0.0 <= prior_prec:
            raise ValueError("Invalid prior precision value: {}".format(prior_prec))
        if not 0.0 <= prec_init:
            raise ValueError("Invalid initial s value: {}".format(prec_init))
        if not 0.0 <= betas[0] < 1.0:
            raise ValueError("Invalid beta parameter at index 0: {}".format(betas[0]))
        if not 0.0 <= betas[1] < 1.0:
            raise ValueError("Invalid beta parameter at index 1: {}".format(betas[1]))
        if num_samples < 1:
            raise ValueError("Invalid num_samples parameter: {}".format(num_samples))
        if train_set_size < 1:
            raise ValueError("Invalid number of training data points: {}".format(train_set_size))
            
        self.num_samples = num_samples
        self.train_set_size = train_set_size

        defaults = dict(lr=lr, betas=betas, prior_prec=prior_prec, prec_init=prec_init)
        super(Vadam, self).__init__(params, defaults)

    def step(self, closure):
        """Performs a single optimization step.
        Arguments:
            closure (callable): A closure that reevaluates the model
                and returns the loss.
        """
        if closure is None:
            raise RuntimeError('For now, Vadam only supports that the model/loss can be reevaluated inside the step function')
            
        grads = []
        grads2 = []
        for group in self.param_groups:
                for p in group['params']:
                    grads.append([])
                    grads2.append([])
        
        # Compute grads and grads2 using num_samples MC samples
        for s in range(self.num_samples):
            
            # Sample noise for each parameter
            pid = 0
            original_values = {}
            for group in self.param_groups:
                for p in group['params']:
    
                    original_values.setdefault(pid, p.detach().clone())
                    state = self.state[p]
                    # State initialization
                    if len(state) == 0:
                        state['step'] = 0
                        # Exponential moving average of gradient values
                        state['exp_avg'] = torch.zeros_like(p.data)
                        # Exponential moving average of squared gradient values
                        state['exp_avg_sq'] = torch.zeros_like(p.data) * (group['prec_init'] - group['prior_prec']) / self.train_set_size
    
                    # A noisy sample
                    raw_noise = torch.normal(mean=torch.zeros_like(p.data), std=1.0)
                    p.data.addcdiv_(1., raw_noise, torch.sqrt(self.train_set_size * state['exp_avg_sq'] + group['prior_prec']))
                    
                    pid = pid + 1
    
            # Call the loss function and do BP to compute gradient
            loss = closure()
            
            # Replace original values and store gradients
            pid = 0
            for group in self.param_groups:
                for p in group['params']:
                    
                    # Restore original parameters
                    p.data = original_values[pid]
    
                    if p.grad is None:
                        continue
                        
                    if p.grad.is_sparse:
                        raise RuntimeError('Vadam does not support sparse gradients')
                    
                    # Aggregate gradients
                    g = p.grad.detach().clone()
                    if s==0:
                        grads[pid] = g
                        grads2[pid] = g**2
                    else:
                        grads[pid] += g
                        grads2[pid] += g**2
                        
                    pid = pid + 1
        
        # Update parameters and states
        pid = 0
        for group in self.param_groups:
            for p in group['params']:

                if grads[pid] is None:
                    continue
                
                # Compute MC estimate of g and g2
                grad = grads[pid].div(self.num_samples)
                grad2 = grads2[pid].div(self.num_samples)
                
                tlambda = group['prior_prec'] / self.train_set_size

                state = self.state[p]

                exp_avg, exp_avg_sq = state['exp_avg'], state['exp_avg_sq']

                beta1, beta2 = group['betas']

                state['step'] += 1

                # Decay the first and second moment running average coefficient
                exp_avg.mul_(beta1).add_(1 - beta1, grad + tlambda * original_values[pid])
                exp_avg_sq.mul_(beta2).add_(1 - beta2, grad2)

                bias_correction1 = 1 - beta1 ** state['step']
                bias_correction2 = 1 - beta2 ** state['step']

                numerator = exp_avg.div(bias_correction1)
                denominator = exp_avg_sq.div(bias_correction2).sqrt().add(tlambda)
                
                # Update parameters
                p.data.addcdiv_(-group['lr'], numerator, denominator)
                
                pid = pid + 1

        return loss

    def get_weight_precs(self, ret_numpy=False):
        """Returns the posterior weight precisions.
        Arguments:
            ret_numpy (bool): If true, the returned list contains numpy arrays,
                otherwise it contains torch tensors.
        """
        weight_precs = []
        for group in self.param_groups:
            weight_prec = []
            for p in group['params']:
                state = self.state[p]
                prec = self.train_set_size * state['exp_avg_sq'] + group['prior_prec']
                if ret_numpy:
                    prec = prec.cpu().numpy()
                weight_prec.append(prec)
            weight_precs.append(weight_prec)

        return weight_precs

    def get_mc_predictions(self, forward_function, inputs, mc_samples=1, ret_numpy=False, *args, **kwargs):
        """Returns Monte Carlo predictions.
        Arguments:
            forward_function (callable): The forward function of the model
                that takes inputs and returns the outputs.
            inputs (FloatTensor): The inputs to the model.
            mc_samples (int): The number of Monte Carlo samples.
            ret_numpy (bool): If true, the returned list contains numpy arrays,
                otherwise it contains torch tensors.
        """

        predictions = []

        for mc_num in range(mc_samples):

            pid = 0
            original_values = {}
            for group in self.param_groups:
                for p in group['params']:
                    
                    original_values.setdefault(pid, torch.zeros_like(p.data)+p.data)

                    state = self.state[p]
                    # State initialization
                    if len(state) == 0:
                        raise RuntimeError('Optimizer not initialized')

                    # A noisy sample
                    raw_noise = torch.normal(mean=torch.zeros_like(p.data), std=1.0)
                    p.data.addcdiv_(1., raw_noise, torch.sqrt(self.train_set_size * state['exp_avg_sq'] + group['prior_prec']))

                    pid = pid + 1

            # Call the forward computation function
            outputs = forward_function(inputs, *args, **kwargs)
            if ret_numpy:
                outputs = outputs.data.cpu().numpy()
            predictions.append(outputs)

            pid = 0
            for group in self.param_groups:
                for p in group['params']:
                    p.data = original_values[pid]
                    pid = pid + 1

        return predictions

    def _kl_gaussian(self, p_mu, p_sigma, q_mu, q_sigma):
        var_ratio = (p_sigma / q_sigma).pow(2)
        t1 = ((p_mu - q_mu) / q_sigma).pow(2)
        return 0.5 * torch.sum((var_ratio + t1 - 1 - var_ratio.log()))

    def kl_divergence(self):
        """Returns the KL divergence between the variational distribution 
        and the prior.
        """
        kl = 0
        for group in self.param_groups:
            for p in group['params']:
                state = self.state[p]
                prec0 = group['prior_prec']
                prec = self.train_set_size * state['exp_avg_sq'] + group['prior_prec']
                kl += self._kl_gaussian(p_mu = p, 
                                        p_sigma = 1. / torch.sqrt(prec), 
                                        q_mu = 0., 
                                        q_sigma = 1. / math.sqrt(prec0))

        return kl