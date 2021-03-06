#!/usr/bin/env python

"""
Implementation of a twiist like method for anonymous RAD loci
"""

import pandas as pd
import numpy as np
import itertools
import tempfile
import toytree
import copy
import uuid
import glob
import os
from .raxml import Raxml as raxml


class Twiist():
    """ 
    Performs phylo inference across sampled iterations to get weights.
    """
    
    def __init__(
        self, 
        data,
        imap, 
        minmap=None,
        chunksize=20, 
        ntests=100, 
        minsnps=1,
        randomseed=None,
        ):

        ## init random seed
        self.randomseed = randomseed
        np.random.seed(self.randomseed)
        
        ## store attributes
        self.data = data
        self.imap = imap
        self.rmap = {}
        self.results_table = None
        for k,v in self.imap.iteritems():
            for i in v:
                self.rmap[i] = k  
        self.ntests = ntests
        self.chunksize = chunksize
        self.minsnps = minsnps
        
        ## fill mindict
        if not minmap:
            minmap = {i:1 for i in self.imap}
        self.minmap = minmap
        
        ## store all samples for this test
        self.samples = list(itertools.chain(*[i for i in self.imap.values()]))
        
        ## get idxs of loci for this test
        self.idxs = self.get_test_locus_idxs()
                
    
    
    def get_test_locus_idxs(self):
        """ finds loci with sufficient sampling for this test"""

        ## store idx of passing loci
        idxs = []

        ## open handle
        with open(self.data) as indata:
            liter = (indata.read().strip().split("|\n"))

        ## put chunks into a list
        for idx, loc in enumerate(liter):

            ## parse chunk
            lines = loc.split("\n")[:-1]
            names = [i.split()[0] for i in lines]

            ## check coverage
            coverage = 0
            for node in self.imap:
                mincov = self.minmap[node]
                if sum([i in names for i in self.imap[node]]) >= mincov:
                    coverage += 1
            if coverage == 4:
                idxs.append(idx)          

        ## concatenate into a phylip file
        return idxs
    


    def sample_loci(self):
        """ finds loci with sufficient sampling for this test"""

        ## store idx of passing loci
        idxs = np.random.choice(self.idxs, self.ntests)

        ## open handle, make a proper generator to reduce mem
        with open(self.data) as indata:
            liter = (indata.read().strip().split("|\n"))

        ## store data as dict
        seqdata = {i:"" for i in self.samples}

        ## put chunks into a list
        for idx, loc in enumerate(liter):
            if idx in idxs:
                ## parse chunk
                lines = loc.split("\n")[:-1]
                names = [i.split()[0] for i in lines]
                seqs = [i.split()[1] for i in lines]
                dd = {i:j for i,j in zip(names, seqs)}

                ## add data to concatenated seqdict
                for name in seqdata:
                    if name in names:
                        seqdata[name] += dd[name]
                    else:
                        seqdata[name] += "N"*len(seqs[0])
                        
        ## concatenate into a phylip file
        return seqdata
    
    

    def run_tree_inference(self, nexus, idx):
        """
        Write nexus to tmpfile, runs phyml tree inference, and parses
        and returns the resulting tree. 
        """
        ## create a tmpdir for this test
        tmpdir = tempfile.tempdir
        tmpfile = os.path.join(tempfile.NamedTemporaryFile(
            delete=False,
            prefix=str(idx),
            dir=tmpdir,
        ))

        ## write nexus to tmpfile
        tmpfile.write(nexus)
        tmpfile.flush()

        ## infer the tree
        rax = raxml(name=str(idx), data=tmpfile.name, workdir=tmpdir, N=1, T=2)
        rax.run(force=True, block=True, quiet=True)

        ## clean up
        tmpfile.close()

        ## return tree order
        order = get_order(toytree.tree(rax.trees.bestTree))
        return "".join(order)


    
    def run(self, ipyclient):
        """
        parallelize calls to worker function.
        """
        
        ## connect to parallel client
        lbview = ipyclient.load_balanced_view()
        
        ## iterate over tests
        asyncs = []
        for test in xrange(self.ntests): 
            
            ## submit jobs to run
            async = lbview.apply(worker, self)
            asyncs.append(async)
            
        ## wait for jobs to finish
        ipyclient.wait()

        ## check for errors
        for async in asyncs:
            if not async.successful():
                raise Exception("Error: {}".format(async.result()))

        ## return results as df
        results = [i.result() for i in asyncs]
        self.results_table = pd.DataFrame(results)
    
    

    def plot(self):
        """
        return a toyplot barplot of the results table.
        """
        if self.results_table == None:
            return "no results found"
        else:
            bb = self.results_table.sort_values(
                by=["ABCD", "ACBD"], 
                ascending=[False, True],
                )

            ## make a barplot
            import toyplot
            c = toyplot.Canvas(width=600, height=200)
            a = c.cartesian()
            m = a.bars(bb)
            return c, a, m



def worker(self):
    """ 
    Calculates the quartet weights for the test at a random
    subsampled chunk of loci.
    """

    ## subsample loci 
    fullseqs = self.sample_loci()

    ## find all iterations of samples for this quartet
    liters = itertools.product(*self.imap.values())

    ## run tree inference for each iteration of sampledict
    hashval = uuid.uuid4().hex
    weights = []
    for ridx, lidx in enumerate(liters):
        
        ## get subalignment for this iteration and make to nex
        a,b,c,d = lidx
        sub = {}
        for i in lidx:
            if self.rmap[i] == "p1":
                sub["A"] = fullseqs[i]
            elif self.rmap[i] == "p2":
                sub["B"] = fullseqs[i]
            elif self.rmap[i] == "p3":
                sub["C"] = fullseqs[i]
            else:
                sub["D"] = fullseqs[i]
                
        ## write as nexus file
        nex = []
        for tax in list("ABCD"):
            nex.append(">{}         {}".format(tax, sub[tax]))
            
        ## check for too much missing or lack of variants
        nsites, nvar = count_var(nex)

        ## only run test if there's variation present
        if nvar > self.minsnps:
               
            ## format as nexus file
            nexus = "{} {}\n".format(4, len(fullseqs[a])) + "\n".join(nex)    

            ## infer ML tree
            treeorder = self.run_tree_inference(nexus, "{}.{}".format(hashval, ridx))

            ## add to list
            weights.append(treeorder)

    ## cleanup - remove all files with the hash val
    rfiles = glob.glob(os.path.join(tempfile.tempdir, "*{}*".format(hashval)))
    for rfile in rfiles:
        if os.path.exists(rfile):
            os.remove(rfile)

    ## return result as weights for the set topologies.
    trees = ["ABCD", "ACBD", "ADBC"]
    wdict = {i:float(weights.count(i))/len(weights) for i in trees}
    return wdict



def get_order(tre):
    """
    return tree order
    """
    anode = tre.tree&">A"
    sister = anode.get_sisters()[0]
    sisters = (anode.name[1:], sister.name[1:])
    others = [i for i in list("ABCD") if i not in sisters]
    return sorted(sisters) + sorted(others)


def count_var(nex):
    """
    count number of sites with cov=4, and number of variable sites.
    """
    arr = np.array([list(i.split()[-1]) for i in nex])
    miss = np.any(arr=="N", axis=0)
    nomiss = arr[:, ~miss]
    nsnps = np.invert(np.all(nomiss==nomiss[0, :], axis=0)).sum()
    return nomiss.shape[1], nsnps


