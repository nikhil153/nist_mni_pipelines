# -*- coding: utf-8 -*-
#
# @author Vladimir S. FONOV
# @date 
#


import shutil
import os
import sys
import csv
import traceback

# MINC stuff
from ipl.minc_tools import mincTools,mincError


def calc_similarity_stats(input_ground_truth, 
                          input_segmentation, 
                          output_stats=None,
                          relabel=None ):
    '''
    Calculate similarity stats
    '''
    stats={}
    
    stats[ 'sample' ]       = input_segmentation
    stats[ 'ground_truth' ] = input_ground_truth
    
    with mincTools() as m:
        sim = m.execute_w_output( 
            ['volume_gtc_similarity', input_ground_truth, input_segmentation,'--csv'] 
                                 ).rstrip("\n").split(',')
        
        stats['gkappa'] = float(sim[0])
        stats['gtc']    = float(sim[1])
        stats['akappa'] = float(sim[2])
        
        sim = m.execute_w_output( 
            [ 'volume_similarity', input_ground_truth, input_segmentation,'--csv'] 
                                ).split("\n")
        
        ka={}
        se={}
        sp={}
        js={}
        
        for i in sim:
            q=i.split(',')
            if len(q)==5:
                l=int(q[0])

                if relabel is not None:
                    l=relabel[l]

                ka[l] = float( q[1] )
                se[l] = float( q[2] )
                sp[l] = float( q[3] )
                js[l] = float( q[4] )
                
        stats['ka']=ka
        stats['se']=se
        stats['sp']=sp
        stats['js']=js

    if output_stats is not None:
        with open(output_stats,'w') as f:
            f.write("{},{},{},{}\n".format(stats['sample'],stats['gkappa'],stats['gtc'],stats['akappa']))

    return stats

def create_grading_map(
                     output_grading, 
                     output_map, 
                     lin_xfm=None, 
                     nl_xfm=None, 
                     template=None ):
    try:
        with mincTools( verbose=2 ) as m:
            xfm=None
            
            if lin_xfm is not None and nl_xfm is not None:
                xfm=m.tmp('concat.xfm')
                m.xfmconcat([lin_xfm,nl_xfm],xfm)
            elif lin_xfm is not None:
                xfm=lin_xfm
            else:
                xfm=nl_xfm

            m.resample_smooth(output_grading,output_map,
                                transform=xfm,
                                like=template,
                                order=2,
                                datatype='short')
                
    except mincError as e:
        print("Exception in split_labels:{}".format(str(e)))
        traceback.print_exc( file=sys.stdout )
        raise
    except :
        print("Exception in split_labels:{}".format(sys.exc_info()[0]))
        traceback.print_exc( file=sys.stdout)
        raise


def create_error_map(input_ground_truth, 
                     input_segmentation, 
                     output_maps, 
                     lin_xfm=None, 
                     nl_xfm=None, 
                     template=None, 
                     label_list=[] ):
    try:
        with mincTools( verbose=2 ) as m:
            # go over labels and calculate errors per label
            #
            for (i,l) in enumerate(label_list):
                # extract label error
                out=m.tmp(str(l)+'.mnc')
                xfm=None
                
                m.calc([input_segmentation, input_ground_truth],
                       "abs(A[0]-{})<0.5&&abs(A[1]-{})>0.5 || abs(A[0]-{})>0.5&&abs(A[1]-{})<0.5 ? 1:0".format(l,l,l,l),
                       out, datatype='-byte')
                
                if lin_xfm is not None and nl_xfm is not None:
                    xfm=m.tmp(str(l)+'.xfm')
                    m.xfmconcat([lin_xfm,nl_xfm],xfm)
                elif lin_xfm is not None:
                    xfm=lin_xfm
                else:
                    xfm=nl_xfm

                m.resample_smooth(out,output_maps[i],
                                    transform=xfm,
                                    like=template,
                                    order=1,
                                    datatype='byte')
                
    except mincError as e:
        print("Exception in split_labels:{}".format(str(e)))
        traceback.print_exc( file=sys.stdout )
        raise
    except :
        print("Exception in split_labels:{}".format(sys.exc_info()[0]))
        traceback.print_exc( file=sys.stdout)
        raise


def average_error_maps(maps, out_avg):
    try:
        with mincTools( verbose=2 ) as m:
            print("average_error_maps {} {}".format(repr(maps),repr(out_avg)))
            m.average(maps, out_avg, datatype='-short')
    except mincError as e:
        print("Exception in split_labels:{}".format(str(e)))
        traceback.print_exc( file=sys.stdout )
        raise
    except :
        print("Exception in split_labels:{}".format(sys.exc_info()[0]))
        traceback.print_exc( file=sys.stdout)
        raise
    

# kate: space-indent on; indent-width 4; indent-mode python;replace-tabs on;word-wrap-column 80;show-tabs on
