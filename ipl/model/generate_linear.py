import shutil
import os
import sys
import csv
import traceback
import json

# MINC stuff
from ipl.minc_tools             import mincTools,mincError

from ipl.model.structures       import MriDataset, MriTransform, MRIEncoder
from ipl.model.filter           import generate_flip_sample, normalize_sample
from ipl.model.filter           import average_samples,average_stats
from ipl.model.filter           import calculate_diff_bias_field,average_bias_fields
from ipl.model.filter           import resample_and_correct_bias
from ipl.model.registration     import linear_register_step, non_linear_register_step
from ipl.model.registration     import average_transforms
from ipl.model.resample         import concat_resample, concat_resample_nl

from scoop import futures, shared



def generate_linear_average(
    samples,
    initial_model=None,
    output_model=None,
    output_model_sd=None,
    prefix='.',
    options={}
    ):
    """ perform iterative model creation"""

    # use first sample as initial model
    if not initial_model:
        initial_model = samples[0]

    # current estimate of template
    current_model = initial_model
    current_model_sd = None
    transforms=[]
    corr=[]

    bias_fields=[]
    corr_transforms=[]
    corr_samples=[]
    sd=[]

    iterations = options.get('iterations',4)
    cleanup    = options.get('cleanup',False)
    symmetric  = options.get('symmetric',False)
    reg_type   = options.get('reg_type','-lsq12')
    objective  = options.get('objective','-xcorr')
    linreg     = options.get('linreg',None)
    refine     = options.get('refine',False)
    biascorr   = options.get('biascorr',False)
    biasdist   = options.get('biasdist',100)# default for 1.5T
    qc         = options.get('qc',False)
    downsample = options.get('downsample',None)
    use_n4     = options.get('N4',False)
    use_median = options.get('median',False)

    models=[]
    models_sd=[]
    models_bias=[]

    if symmetric:
        flipdir=prefix+os.sep+'flip'
        if not os.path.exists(flipdir):
            os.makedirs(flipdir)

        flip_all=[]
        # generate flipped versions of all scans
        for (i, s) in enumerate(samples):
            _s_name=os.path.basename(s.scan).rsplit('.gz',1)[0]
            s.scan_f=prefix+os.sep+'flip'+os.sep+_s_name

            if s.mask is not None:
                s.mask_f=prefix+os.sep+'flip'+os.sep+'mask_'+_s_name

            flip_all.append( futures.submit( generate_flip_sample,s )  )

        futures.wait(flip_all, return_when=futures.ALL_COMPLETED)

    # go through all the iterations
    for it in xrange(1,iterations+1):

        # this will be a model for next iteration actually

        # 1 register all subjects to current template
        next_model     =MriDataset(prefix=prefix, iter=it, name='avg')
        next_model_sd  =MriDataset(prefix=prefix, iter=it, name='sd')
        next_model_bias=MriDataset(prefix=prefix, iter=it, name='bias')

        transforms=[]

        it_prefix=prefix+os.sep+str(it)
        if not os.path.exists(it_prefix):
            os.makedirs(it_prefix)

        inv_transforms=[]
        fwd_transforms=[]
        for (i, s) in enumerate(samples):
            sample_xfm     = MriTransform(name=s.name, prefix=it_prefix,iter=it,linear=True)
            sample_inv_xfm = MriTransform(name=s.name+'_inv', prefix=it_prefix,iter=it,linear=True)

            prev_transform = None
            prev_bias_field = None

            if it > 1 and refine:
                prev_transform = corr_transforms[i]

            if it > 1 and biascorr:
                prev_bias_field = bias_fields[i]


            transforms.append(
                futures.submit(
                    linear_register_step,
                    s,
                    current_model,
                    sample_xfm,
                    output_invert=sample_inv_xfm,
                    init_xfm=prev_transform,
                    symmetric=symmetric,
                    reg_type=reg_type,
                    objective=objective,
                    linreg=linreg,
                    work_dir=prefix,
                    bias=prev_bias_field,
                    downsample=downsample)
                )
            inv_transforms.append(sample_inv_xfm)
            fwd_transforms.append(sample_xfm)


        # wait for jobs to finish
        futures.wait(transforms, return_when=futures.ALL_COMPLETED)
    
        # remove information from previous iteration
        if cleanup and it>1 :
            for s in corr_samples:
               s.cleanup(verbose=True)
            for x in corr_transforms:
               x.cleanup(verbose=True)

        # here all the transforms should exist
        avg_inv_transform=MriTransform(name='avg_inv', prefix=it_prefix,iter=it,linear=True)

        # 2 average all transformations
        result=futures.submit(
            average_transforms, inv_transforms, avg_inv_transform, nl=False, symmetric=symmetric
            # TODO: maybe make median transforms?
            )
        futures.wait([result], return_when=futures.ALL_COMPLETED)

        corr=[]
        corr_transforms=[]
        corr_samples=[]
        # 3 concatenate correction and resample

        for (i, s) in enumerate(samples):
            prev_bias_field = None
            if it > 1 and biascorr:
                prev_bias_field = bias_fields[i]

            c=MriDataset(  prefix=it_prefix,iter=it,name=s.name)
            x=MriTransform(name=s.name+'_corr',prefix=it_prefix,iter=it,linear=True)
            
            corr.append(futures.submit( 
                concat_resample, s, fwd_transforms[i], avg_inv_transform, 
                c, x, current_model, symmetric=symmetric, qc=qc, bias=prev_bias_field 
                ))
            corr_transforms.append(x)
            corr_samples.append(c)
        futures.wait(corr, return_when=futures.ALL_COMPLETED)

        # cleanup transforms
        if cleanup :
            for x in inv_transforms:
                x.cleanup()
            for x in fwd_transforms:
                x.cleanup()
            avg_inv_transform.cleanup()
            
        # 4 average resampled samples to create new estimate
        result=futures.submit(
            average_samples, corr_samples, next_model, next_model_sd, symmetric=symmetric, symmetrize=symmetric,median=use_median
            )

        if cleanup :
            # remove previous template estimate
            models.append(next_model)
            models_sd.append(next_model_sd)

        futures.wait([result], return_when=futures.ALL_COMPLETED)

        if biascorr:
            biascorr_results=[]
            new_bias_fields=[]

            for (i, s) in enumerate(samples):
                prev_bias_field = None
                if it > 1:
                    prev_bias_field = bias_fields[i]
                c=corr_samples[i]
                x=corr_transforms[i]
                b=MriDataset(prefix=it_prefix,iter=it,name='bias_'+s.name)
                biascorr_results.append( futures.submit( 
                    calculate_diff_bias_field, 
                        c, next_model, b, symmetric=symmetric, distance=biasdist,
                        n4=use_n4
                    ) )
                new_bias_fields.append(b)

            futures.wait(biascorr_results, return_when=futures.ALL_COMPLETED)

            result=futures.submit( 
                average_bias_fields, new_bias_fields, next_model_bias, symmetric=symmetric
                )
            futures.wait([result], return_when=futures.ALL_COMPLETED)
            biascorr_results=[]
            new_corr_bias_fields=[]
            for (i, s) in enumerate(samples):
                prev_bias_field = None
                if it > 1:
                    prev_bias_field = bias_fields[i]
                c=corr_samples[i]
                x=corr_transforms[i]
                b=new_bias_fields[i]
                out=MriDataset(prefix=it_prefix,iter=it,name='c_bias_'+s.name)
                biascorr_results.append( futures.submit( 
                    resample_and_correct_bias, b, x , next_model_bias, out, previous=prev_bias_field, symmetric=symmetric 
                    ) )
                new_corr_bias_fields.append( out )
            futures.wait(biascorr_results, return_when=futures.ALL_COMPLETED)

        # swap bias fields
        if biascorr: bias_fields=new_bias_fields
        
        current_model=next_model
        current_model_sd=next_model_sd
        sd.append( futures.submit(average_stats, next_model, next_model_sd ) )

    # copy output to the destination
    futures.wait(sd, return_when=futures.ALL_COMPLETED)

    with open(prefix+os.sep+'stats.txt','w') as f:
        for s in sd:
            f.write("{}\n".format(s.result()))

    if cleanup:
        # keep the final model
        models.pop()
        models_sd.pop()
        
        # delete unneeded models
        for m in models:
            m.cleanup()
        for m in models_sd:
            m.cleanup()
            
    results={
            'model':     current_model,
            'model_sd':  current_model_sd,
            'xfm':       corr_transforms,
            'biascorr':  bias_fields,
            'scan':      corr_samples,
            'symmetric': symmetric
            }

    with open(prefix+os.sep+'results.json','w') as f:
         json.dump(results,f,indent=1,cls=MRIEncoder)
    
    return results



def generate_linear_model(samples,model=None,mask=None,work_prefix=None,options={}):
    internal_sample=[]

    try:
        for i in samples:
            s=MriDataset(scan=i[0],mask=i[1])
            internal_sample.append(s)

        internal_model=None
        if model is not None:
            internal_model=MriDataset(scan=model,mask=mask)

        if work_prefix is not None and not os.path.exists(work_prefix):
            os.makedirs(work_prefix)

        return generate_linear_average(internal_sample,internal_model,prefix=work_prefix,options=options)
    except mincError as e:
        print "Exception in generate_linear_model:{}".format(str(e))
        traceback.print_exc(file=sys.stdout)
        raise
    except :
        print "Exception in generate_linear_model:{}".format(sys.exc_info()[0])
        traceback.print_exc(file=sys.stdout)
        raise

def generate_linear_model_csv(input_csv,model=None,mask=None,work_prefix=None,options={}):
    internal_sample=[]

    with open(input_csv, 'r') as csvfile:
        reader = csv.reader(csvfile, delimiter=',', quoting=csv.QUOTE_NONE)
        for row in reader:
            internal_sample.append(MriDataset(scan=row[0],mask=row[1]))

    internal_model=None
    if model is not None:
        internal_model=MriDataset(scan=model,mask=mask)

    if work_prefix is not None and not os.path.exists(work_prefix):
        os.makedirs(work_prefix)

    return generate_linear_average(internal_sample,internal_model,prefix=work_prefix,options=options)
 
# kate: space-indent on; indent-width 4; indent-mode python;replace-tabs on;word-wrap-column 80;show-tabs on
