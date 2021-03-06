from os.path import join
from random import randint
from itertools import izip
from datetime import datetime

import dill
import numpy as np
from sklearn import svm
from scipy.optimize import minimize, check_grad as cg, approx_fprime

from crf import ChainCRF
from data import read_gesture
from evaluation import Evaluator
from utils import timed, mkdir_p, pmap


def test_and_save(train):
    """ sweet deco which adds auto testing+saving """
    def _train_test_save(self, W=None, rand=False, path='', name='', *a, **k):
        if not hasattr(self, 'W_opt'):
            self.W_init(W, rand)
        outs = train(self, *a, **k), self.test(), self.save_solution(path, name)
        return zip(('tr', 'te', 'sav'), outs)
    return _train_test_save


class Learner:
    def __init__(self, crf, gibbs=False, cd=False, n_samps=5, burn=5, interval=5):
        self.crf = crf
        self.gibbs = gibbs
        self.cd = gibbs and cd
        self.E_f = self.exp_feat_gibbs if gibbs else self.exp_feat
        self.n_samples = n_samps
        self.burn = burn
        self.interval = interval
        self.ev = Evaluator()

    def feat(self, x, y):
        node_feats = np.zeros(self.crf.U_shape)
        edge_feats = np.zeros(self.crf.B_shape)
        V = range(len(x))
        for i in V:
            node_feats[y[i]] += x[i]
        for i, j in izip(V, V[1:]):
            edge_feats[y[i], y[j]] += self.crf.edge_feat(x, i, j, y[i], y[j])
        return np.concatenate((node_feats.flatten(), edge_feats.flatten()))

    def exp_feat(self, x, W):
        node_feats = np.zeros(self.crf.U_shape)
        edge_feats = np.zeros(self.crf.B_shape)
        V, Z = range(len(x)), self.crf.Z(x, *W)
        for i in V:
            for l in self.crf.L:
                node_feats[l] += np.exp(self.crf.marginal(x, W, [(i, l)]) - Z) * x[i]
        for i, j in izip(V, V[1:]):
            for m, n in self.crf.configs(2):
                ef = self.crf.edge_feat(x, i, j, m, n)
                edge_feats[m, n] += np.exp(self.crf.marginal(x,W,[(i,m),(j,n)]) - Z) * ef
        return np.concatenate((node_feats.flatten(), edge_feats.flatten()))

    def exp_feat_gibbs(self, x, W, y=None):
        S = self.crf.Gibbs(x, W, self.n_samples, self.burn, self.interval, init=y)
        feats = np.zeros(self.crf.n_W)
        for s in S:
            feats += self.feat(x, s)
        return feats / self.n_samples

    def regularize(self, reg):
        def obj(W):
            return self.obj(W) + reg * np.linalg.norm(W)**2
        def grad(W, *a):
            return self.grad(W, *a) + 2. * reg * W
        return obj, grad

    def check_grad(self, N=1):
        Ws = [np.random.rand(self.crf.n_W) for _ in xrange(N)]
        return [cg(self.obj, self.grad, W) for W in Ws]

    def grad_apx(self, W):
        return approx_fprime(W, self.obj, np.sqrt(np.finfo(float).eps))

    def save_solution(self, path='', name=''):
        """ NOTE must have trained already """
        path = join('results', path, name + datetime.now().strftime('%Y-%-m-%-d_%-H-%-M-%-S'))
        mkdir_p(path)
        print '\n[DONE] Made results folder: %s' % path
        if hasattr(self, 'W_opt') and hasattr(self, 'train_time'):
            print '\t[INFO] Serializing W_opt'
            np.save(join(path, 'W_opt_{:.2f}'.format(self.train_time)), self.W_opt)
        for s in 'val', 'test':
            if hasattr(self, s + '_loss'):
                print '\t[INFO] Serializing %s_loss array' % s
                np.save(join(path, s + '_loss'), getattr(self, s + '_loss'))
        if hasattr(self, 'Ws_val'):
            print '\t[INFO] Serializing Ws_val'
            np.save(join(path, 'Ws_val'), self.Ws_val)
        return path

    def W_init(self, W=None, rand=False):
        self.W_opt = (np.random.rand if rand else np.zeros)(self.crf.n_W) if W is None else W
        return self.W_opt

    def test(self):
        Ws = self.crf.split_W(self.W_opt)
        with timed('TEST SET - MAP Prediction'):
            self.test_loss = self.ev(self.crf.Y_t, [self.crf.MAP(x, Ws) for x in self.crf.X_t])
        print '\tLOSS: %s' % self.ev.get_names(self.test_loss)
        return self.test_loss

    @test_and_save
    def train(self, reg=.9, method='L-BFGS-B', disp=True, maxiter=100):
        """
        if implementing self.{obj(W),grad(W)} to be used with scipy optimization 
        """
        print '[START] SML/SGD Training\n\nTR/VAL/TE SIZES: %s\n' % self.crf.Ns
        obj, grad = self.regularize(reg) if reg > 0 else (self.obj, self.grad)
        self.val_loss, self.Ws_val = [], []
        def val(W):
            print 'current W - obj: %s, norm: %s' % (obj(W), np.linalg.norm(W))
            Ws = self.crf.split_W(W)
            with timed('Validation (MAP predict)', skip=''):
                loss = self.ev(self.crf.Y_v, [self.crf.MAP(x, Ws) for x in self.crf.X_v])
            print '\tVAL LOSS: %s\n' % self.ev.get_names(loss)
            self.val_loss.append(loss)
            self.Ws_val.append(np.array(W))
        with timed('Scipy Opt: %s' % method, self):
            try:
                self.opt = minimize(obj, self.W_opt, method=method, jac=grad, callback=val,
                    options={'maxiter': maxiter, 'disp': disp})
                self.W_opt = self.opt.x
            except KeyboardInterrupt:
                print '\nINFO - Manually exited Scipy training'
        return self.W_opt, self.val_loss, self.Ws_val


class ML(Learner):
    """
    Maximum Likelihood
    """
    def obj(self, W):
        Ws = self.crf.split_W(W)
        return sum(self.crf.E(x, y, Ws) + self.crf.Z(x, *Ws) for x, y in self.crf.XY())

    def grad(self, W):
        Ws, feats = self.crf.split_W(W), np.zeros(self.crf.n_W)
        for x, y in self.crf.XY():
            feats += self.feat(x, y) - self.E_f(x, Ws)
        return feats


# TODO FIXME
class PL(Learner):
    """
    Pseudo-Likelihood
    """
    def exp_feat_PL(self, x, y, W, i):
        s, Z = 0., self.crf.Z_PL(x, y, W, i)
        for l in self.crf.L:
            p = np.exp(self.crf.E_PL(x,y,W,i,l) - Z)
            f = self.feat(x, self.crf.set_idxs(y, i, l))
            s += p * f
        return s

    def obj(self, W):
        Ws = self.crf.split_W(W)
        s = 0.
        for x, y in self.crf.XY():
            s += self.crf.E(x, y, Ws) # XXX inside inner loop?
            for i in xrange(len(x)):
                s += self.crf.Z_PL(x, y, Ws, i)
        return s

    def grad(self, W):
        Ws = self.crf.split_W(W)
        s = 0.
        for x, y in self.crf.XY():
            s += self.feat(x, y) # XXX inside inner loop?
            for i in xrange(len(x)):
                s -= self.exp_feat_PL(x, y, Ws, i)
        return s


class SML(Learner):
    """
    Stochastic Maximum Likelihood
    """
    def grad(self, W, x, y):
        Ws = self.crf.split_W(W)
        exp_f = self.E_f(x, Ws, y) if self.cd else self.E_f(x, Ws)
        return self.feat(x, y) - exp_f

    @test_and_save
    def sgd(self, reg=.9, lr_init=1., step=2, n_iters=300000, val_interval=5000):
        """
        when using Gibbs approximation and the current y is passed to the gradient function,
        this will perform CD-k (i.e., starting the gibbs sampler from the current y, and
        sampling one example after a burn-in time of k steps, typically 1)
        """
        print '[START] SML/SGD Training\n\nTR/VAL/TE SIZES: %s\n' % self.crf.Ns
        grad = self.regularize(reg)[1] if reg > 0 else self.grad
        self.val_loss, self.Ws_val, lr = [], [], lr_init
        with timed('SML/SGD', self):
            try:
                for i in xrange(1, n_iters+1):
                    r = randint(0, self.crf.N_tr - 1)
                    g = grad(self.W_opt, self.crf.X[r], self.crf.Y[r])
                    self.W_opt -= lr * g
                    print 'Iteration #%s: lr=%s, |grad|=%s' % (i, lr, np.linalg.norm(g))
                    if step:
                        lr = lr_init * np.power(.1, np.floor(i * (step+1) / n_iters))
                    if i % val_interval == 0:
                        print '\nCurrent norm: |W| = %s' % np.linalg.norm(self.W_opt)
                        Ws = self.crf.split_W(self.W_opt)
                        with timed('Validation Iter (MAP predict)', skip=''):
                            loss = self.ev(self.crf.Y_v,
                                [self.crf.MAP(x,Ws) for x in self.crf.X_v])
                        print '\tVAL LOSS: %s\n' % self.ev.get_names(loss)
                        self.val_loss.append(loss)
                        self.Ws_val.append(np.array(self.W_opt))
            except KeyboardInterrupt:
                print '\nINFO - Manually exited train loop at Iteration %s' % i
        return self.W_opt, self.val_loss, self.Ws_val


# TODO FIXME log to file so stdout isn't crowded
def train_gesture(cores=20, n_batches=20):
    def train1((X, Y, V, labels, name)):
        crf = ChainCRF(X, Y, labels, V=V, test_pct=.365, val_pct=.4)
        sml = SML(crf, gibbs=True, cd=True, n_samps=5, burn=5, interval=5)
        sml.sgd(n_iters=500000, val_interval=10000, rand=True, 
                path='Gesture_SML_reg9_cd555_iter500k', name=name[:-4])
        return sml.test_loss
    test_loss = pmap(train1, list(read_gesture(n_batches=n_batches)), n_jobs=cores) \
        if cores > 1 else [train1(attrs) for attrs in read_gesture(n_batches=n_batches)]
    avg_loss = map(np.mean, zip(*test_loss))
    print '\nAggregated Mean Test Loss: %s' % avg_loss
    return avg_loss


def train_svc(crf, C=1., loss='squared_hinge', penalty='l2'):
    X, Y = np.concatenate(crf.X), np.concatenate(crf.Y)
    dual = crf.N_tr <= crf.n_feats
    return svm.LinearSVC(penalty, loss, dual, C=C).fit(X, Y)


# TODO parallelize
def train_svc_multiple(crf, name=''):
    mkdir_p('SVC_results_%s' % name)
    for c in .1, 1., 10., 100.:
        for l in 'squared_hinge', 'hinge':
            for p in 'l1', 'l2':
                try:
                    svc = train_svc(crf, C=c, loss=l, penalty=p)
                    with open('SVC_results_%s/svc_%s_%s_%s.p' % (name, c, l, p), 'wb') as f:
                        dill.dump(svc, f)
                except:
                    print '\n[WARNING] unsupported set of SVC args\n'


if __name__ == '__main__':
    ## import data, CRF model
    from data import potts, synthetic

    ## make data
    nl = 5
    data = synthetic(300, lims=(4, 8), n_feats=10, n_labels=nl)
    X, Y = zip(*data)

    ## construct CRF on data
    # crf = ChainCRF(X, Y, range(nl))
    crf = ChainCRF(X, Y, range(nl), potts(nl))

    ## construct learner on crf/data and train using RMCL/LBFGS
    learner = ML(crf)
    ml = ML(crf, gibbs=True, n_samps=10, burn=100, interval=10)
    ml.train(rand=True, path='save_path')

    ## construct random parameter vector and compute objective/gradient/finite-diff
    ## then check gradient against finite-diff
    W = np.random.rand(crf.n_W)
    Ws = crf.split_W(W)
    print learner.obj(W)
    print learner.grad(W)
    print learner.grad_apx(W)
    print learner.check_grad()

    ## construct learner on crf/data and train using SML (SGD+Gibbs sampling)
    learner = SML(crf, gibbs=True, cd=True, n_samps=1, burn=0, interval=1)
    learner.sgd(reg=.8, rand=True, path='save_path2')