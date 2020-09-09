# Copyright 2018 The TensorFlow Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Functions for reading and preprocessing light curves."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import sys
import os
import traceback

from absl import logging
import numpy as np
import astropy
import tensorflow as tf

from light_curve_util import keplersplinev2
from light_curve_util import median_filter
from light_curve_util import median_filter2
from light_curve_util import util
from light_curve_util import tess_io
from statsmodels.robust import scale


def read_and_process_light_curve(tic, tess_data_dir, flux_key='KSPSAP_FLUX'):
  file_names = tess_io.tess_filenames(tic, tess_data_dir)
  assert file_names
  all_time, all_mag = tess_io.read_tess_light_curve(file_names, flux_key)
  assert len(all_time)
  return all_time, all_mag


def get_spline_mask(time, period, t0, tdur):
  phase = util.phase_fold_time(time, period, t0)
  outtran = (np.abs(phase) > tdur / 2)
  return outtran

def filter_outliers(time, flux):
  valid = ~np.isnan(flux)
  time, flux = time[valid], flux[valid]
  return time, flux


def detrend_and_filter(tic_id, time, flux, period, epoch, duration):
  input_mask = get_spline_mask(time, period, epoch, duration)
  spline_flux = keplersplinev2.choosekeplersplinev2(time, flux, input_mask=input_mask)
  detrended_flux = flux / spline_flux
  return filter_outliers(time, detrended_flux)


def phase_fold_and_sort_light_curve(time, flux, period, t0):
  """Phase folds a light curve and sorts by ascending time.

  Args:
    time: 1D NumPy array of time values.
    flux: 1D NumPy array of flux values.
    period: A positive real scalar; the period to fold over.
    t0: The center of the resulting folded vector; this value is mapped to 0.

  Returns:
    folded_time: 1D NumPy array of phase folded time values in
        [-period / 2, period / 2), where 0 corresponds to t0 in the original
        time array. Values are sorted in ascending order.
    folded_flux: 1D NumPy array. Values are the same as the original input
        array, but sorted by folded_time.
  """
  # Phase fold time.
  time = util.phase_fold_time(time, period, t0)

  # Sort by ascending time.
  sorted_i = np.argsort(time)
  time = time[sorted_i]
  flux = flux[sorted_i]

  return time, flux


def generate_view(tic_id, time, flux, period, num_bins, bin_width, t_min, t_max,
                  normalize=True, new_binning=True):
  """Generates a view of a phase-folded light curve using a median filter.

  Args:
    time: 1D array of time values, phase folded and sorted in ascending order.
    flux: 1D array of flux values.
    num_bins: The number of intervals to divide the time axis into.
    bin_width: The width of each bin on the time axis.
    t_min: The inclusive leftmost value to consider on the time axis.
    t_max: The exclusive rightmost value to consider on the time axis.
    normalize: Whether to center the median at 1 and minimum value at 0.

  Returns:
    1D NumPy array of size num_bins containing the median flux values of
    uniformly spaced bins on the phase-folded time axis.
  """
  try:
    if new_binning:
      view = median_filter2.new_binning(time, flux, period, num_bins, t_min, t_max)
    else:
      view = median_filter.median_filter(time, flux, num_bins, bin_width, t_min, t_max)
  except:
    logging.warning("Robust mean failed for %s, using median", tic_id)
    view = median_filter.median_filter(time, flux, num_bins, bin_width, t_min, t_max)

  if normalize:
    view -= np.median(view)
    scale = np.abs(np.min(view))
    # In pathological cases, min(view) is zero...
    scale = np.where(scale != 0, scale, np.ones_like(scale))
    view /= scale
    

  return view


def global_view(tic_id, time, flux, period, num_bins=201, bin_width_factor=1.2/201, new_binning=True):
  """Generates a 'global view' of a phase folded light curve.

  See Section 3.3 of Shallue & Vanderburg, 2018, The Astronomical Journal.
  http://iopscience.iop.org/article/10.3847/1538-3881/aa9e09/meta

  Args:
    time: 1D array of time values, sorted in ascending order.
    flux: 1D array of flux values.
    period: The period of the event (in days).
    num_bins: The number of intervals to divide the time axis into.
    bin_width_factor: Width of the bins, as a fraction of period.

  Returns:
    1D NumPy array of size num_bins containing the median flux values of
    uniformly spaced bins on the phase-folded time axis.
  """
  return generate_view(
      tic_id, 
      time,
      flux,
      period,
      num_bins=num_bins,
      bin_width=period * bin_width_factor,
      t_min=-period / 2,
      t_max=period / 2,
      new_binning=new_binning)


def twice_global_view(time, flux, period, num_bins=402, bin_width_factor=1.2 / 402):
  """Generates a 'global view' of a phase folded light curve at 2x the BLS period.

  See Section 3.3 of Shallue & Vanderburg, 2018, The Astronomical Journal.
  http://iopscience.iop.org/article/10.3847/1538-3881/aa9e09/meta
  If single transit, this is pretty much identical to global_view.

  Args:
    time: 1D array of time values, sorted in ascending order, phase-folded at 2x period.
    flux: 1D array of flux values.
    period: The period of the event (in days).
    num_bins: The number of intervals to divide the time axis into.
    bin_width_factor: Width of the bins, as a fraction of period.

  Returns:
    1D NumPy array of size num_bins containing the median flux values of
    uniformly spaced bins on the phase-folded time axis.
  """
  return generate_view(
      tic_id, 
      time,
      flux,
      period,
      num_bins=num_bins,
      bin_width=period * bin_width_factor,
      t_min=-period,
      t_max=period)


def local_view(tic_id, 
               time,
               flux,
               period,
               duration,
               num_bins=61,
               bin_width_factor=0.16,
               num_durations=2,
               new_binning=True):
  """Generates a 'local view' of a phase folded light curve.
  See Section 3.3 of Shallue & Vanderburg, 2018, The Astronomical Journal.
  http://iopscience.iop.org/article/10.3847/1538-3881/aa9e09/meta
  Args:
    time: 1D array of time values, sorted in ascending order.
    flux: 1D array of flux values.
    period: The period of the event (in days).
    duration: The duration of the event (in days).
    num_bins: The number of intervals to divide the time axis into.
    bin_width_factor: Width of the bins, as a fraction of duration.
    num_durations: The number of durations to consider on either side of 0 (the
        event is assumed to be centered at 0).
  Returns:
    1D NumPy array of size num_bins containing the median flux values of
    uniformly spaced bins on the phase-folded time axis.
  """
  return generate_view(
      tic_id, 
      time,
      flux,
      period,
      num_bins=num_bins,
      bin_width=duration * bin_width_factor,
      t_min=max(-period / 2, -duration * num_durations),
      t_max=min(period / 2, duration * num_durations),
      new_binning=new_binning)


def mask_transit(time, duration, period, mask_width=2, phase_limit=0.1):
    mask = [(abs(t) > duration * mask_width / 2) and (abs(t) > period * phase_limit) for t in time]
    return np.array(mask)


def find_secondary(time, flux, duration, period, mask_width=2, phase_limit=0.1):
    """
    Mask out transits, rearrange LC such that time goes from 0 to period. Then perform grid search for most likely
    secondary eclipse. To be called after preprocess.phase_fold_and_sort_light_curve. OOT flux should be 1.
    :param time: 1D array of time values, folded and sorted in ascending order, with the transit located at time 0.
    :param flux: 1D array of fluxes.
    :param duration: The duration of the event (in days).
    :param period: the period of the event (in days).
    :param mask_width: number of durations to mask out.
    :param phase_limit: minimum phase to search for secondary eclipse.
    :return: time of centre of most likely secondary.
    """
    if period < 1:
        mask_width = 1

    mask = mask_transit(time, duration, period, mask_width, phase_limit)
    if not any(mask):
        mask = mask_transit(time, duration, period, mask_width / 2, phase_limit)

    new_time = time[mask]
    new_flux = flux[mask]

    # rearrange so that time goes from 0 to period
    new_time[new_time < 0] += period
    new_index = np.argsort(new_time)
    new_time = new_time[new_index]
    new_flux = new_flux[new_index]
    new_flux -= 1.  # centre flux at zero

    # grid search for secondary. Fix duration to duration of primary.
    time_grid = np.arange(new_time[0]+duration, new_time[-1]-duration, duration*0.1)
    min_index = 0
    max_index = min_index
    best_t0 = period / 2
    best_SR = 0

    for t0 in time_grid:
        while new_time[min_index] < (t0 - duration):
            min_index += 1
        min_in_transit = min_index
        max_in_transit = min_in_transit
        while (new_time[max_index] < (t0 + duration)) and (max_index < len(new_time)):
            max_index += 1
        while new_time[min_in_transit] < (t0 - duration/2):
            min_in_transit += 1
        while new_time[max_in_transit] < (t0 + duration/2):
            max_in_transit += 1
        if max_index - min_index < 5:
            continue
        r = float(max_in_transit - min_in_transit + 1) / len(new_time)  # assuming identical uniform weights
        s = sum(new_flux[min_in_transit:max_in_transit] / float(len(new_time)))

        SR = s**2 / (r*(1-r))
        if SR > best_SR:
            best_t0 = t0
            best_SR = SR
    return best_t0, new_time, new_flux+1.


def secondary_view(tic_id, 
                   time,
                   flux,
                   period,
                   duration,
                   num_bins=61,
                   bin_width_factor=0.16,
                   num_durations=4
                  ):
    """Generates a 'local view' of a phase folded light curve, centered on phase 0.5.
      See Section 3.3 of Shallue & Vanderburg, 2018, The Astronomical Journal.
      http://iopscience.iop.org/article/10.3847/1538-3881/aa9e09/meta
      Args:
        time: 1D array of time values, sorted in ascending order, with the transit located at time 0.
        flux: 1D array of flux values.
        period: The period of the event (in days).
        duration: The duration of the event (in days).
        num_bins: The number of intervals to divide the time axis into.
        bin_width_factor: Width of the bins, as a fraction of duration.
        num_durations: The number of durations to consider on either side of 0 (the
            event is assumed to be centered at 0).
      Returns:
        1D NumPy array of size num_bins containing the median flux values of
        uniformly spaced bins on the phase-folded time axis.
      """
    
    t0, new_time, new_flux = find_secondary(time, flux, duration, period)
    
    t_min = max(t0 - period / 2, t0 - duration * num_durations, new_time[0])
    t_max = min(t0 + period / 2, t0 + duration * num_durations, new_time[-1])

    if bin_width_factor * duration < (t_max - t_min):
        bin_width = bin_width_factor * duration
    else:
        bin_width = (t_max - t_min) / 40

    return generate_view(
        tic_id, 
      new_time,
        new_flux,
        period,
        num_bins=num_bins,
        bin_width=bin_width,
        t_min=t_min,
        t_max=t_max
    )
