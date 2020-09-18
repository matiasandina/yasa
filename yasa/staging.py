"""
Automatic sleep staging of polysomnography data.
"""
import joblib
import logging
import numpy as np
import pandas as pd
import entropy as ent
import scipy.signal as sp_sig
import scipy.stats as sp_stats
from mne.filter import filter_data
from sklearn.preprocessing import robust_scale

from .spectral import bandpower_from_psd_ndarray
from .others import sliding_window, _zerocrossings

logger = logging.getLogger('yasa')


class SleepStaging:
    """
    Automatic sleep staging of polysomnography data.

    Parameters
    ----------
    eeg : array_like
        Single-channel EEG data. Preferentially the C4-M1 or C3-M2 derivation.
    sf : float
        Sampling frequency of the data in Hz.
    metadata : dict or None
        A dictionary of metadata. Currently supported keys are:

        * ``'age'``: Age of the participant, in years.
        * ``'male'``: Sex of the participant (1 or True = male, 0 or
          False = female)
    """

    def __init__(self, eeg, sf, metadata=None):
        # Type checks
        assert isinstance(sf, (int, float)), "sf must be an int or a float."
        assert isinstance(metadata, (dict, type(None))
                          ), "metadata must be a dict or None"

        # Validate metadata
        if isinstance(metadata, dict):
            if 'age' in metadata.keys():
                assert 0 < metadata['age'] < 120, ('age must be between 0 and '
                                                   '120.')
            if 'male' in metadata.keys():
                metadata['male'] = int(metadata['male'])
                assert metadata['male'] in [0, 1], 'male must be 0 or 1.'

        # Validate EEG data
        eeg = np.squeeze(np.asarray(eeg, dtype=np.float64))
        assert eeg.ndim == 1, 'Only single-channel EEG data is supported.'
        duration_minutes = eeg.size / sf / 60
        assert duration_minutes >= 5, 'At least 5 minutes of data is required.'

        # Validate sampling frequency
        assert sf > 80, 'Sampling frequency must be at least 80 Hz.'
        if sf >= 1000:
            logger.warning(
                'Very high sampling frequency (sf >= 1000 Hz) can '
                'significantly reduce computation time. For faster execution, '
                'please downsample your data to the 100-500Hz range.'
            )

        # Add to self
        self.eeg = eeg
        self.sf = sf
        self.metadata = metadata

    def fit(self, freq_broad=(0.5, 40), win_sec=4):
        """Extract features from data.
        
        Parameters
        ----------
        freq_broad : tuple or list
            Broad band frequency range. Default is 0.5 to 40 Hz.
        win_sec : int or float
            The length of the sliding window, in seconds, used for the Welch PSD
            calculation. Ideally, this should be at least two times the inverse of
            the lower frequency of interest (e.g. for a lower frequency of interest
            of 0.5 Hz, the window length should be at least 2 * 1 / 0.5 =
            4 seconds).

        Returns
        -------
        self : returns an instance of self.
        """
        # 1) Preprocessing
        # - Filter the data
        eeg_filt = filter_data(
            self.eeg, self.sf, l_freq=freq_broad[0], h_freq=freq_broad[1],
            verbose=False)
        # - Extract 30 sec epochs. Data is now of shape (n_epochs, n_samples).
        times, eeg_ep = sliding_window(eeg_filt, sf=self.sf, window=30)

        # 2) Calculate standard descriptive statistics
        perc = np.percentile(eeg_ep, q=[10, 90], axis=1)
        features = {
            'time_hour': times / 3600,
            'time_norm': times / times[-1],
            'eeg_absmean': np.abs(eeg_ep).mean(axis=1),
            'eeg_std': eeg_ep.std(ddof=1, axis=1),
            'eeg_10p': perc[0],
            'eeg_90p': perc[1],
            'eeg_iqr': sp_stats.iqr(eeg_ep, axis=1),
            'eeg_skew': sp_stats.skew(eeg_ep, axis=1),
            'eeg_kurt': sp_stats.kurtosis(eeg_ep, axis=1)
        }

        # 3) Calculate spectral power features
        win = int(win_sec * self.sf)
        freqs, psd = sp_sig.welch(
            eeg_ep, self.sf, window='hamming', nperseg=win, average='median')
        bp = bandpower_from_psd_ndarray(psd, freqs)
        bands = ['delta', 'theta', 'alpha', 'sigma', 'beta', 'gamma']
        for i, b in enumerate(bands):
            features['eeg_' + b] = bp[i]

        # Add power ratios
        features['eeg_dt'] = features['eeg_delta'] / features['eeg_theta']
        features['eeg_ds'] = features['eeg_delta'] / features['eeg_sigma']
        features['eeg_db'] = features['eeg_delta'] / features['eeg_beta']
        features['eeg_at'] = features['eeg_alpha'] / features['eeg_theta']

        # Add total power
        idx_broad = np.logical_and(
            freqs >= freq_broad[0], freqs <= freq_broad[1])
        dx = freqs[1] - freqs[0]
        features['eeg_abspow'] = np.trapz(psd[:, idx_broad], dx=dx)

        # 4) Calculate entropy features
        features['eeg_perm'] = np.apply_along_axis(
            ent.perm_entropy, axis=1, arr=eeg_ep, normalize=True)
        features['eeg_higuchi'] = np.apply_along_axis(
            ent.higuchi_fd, axis=1, arr=eeg_ep)
        features['eeg_nzc'] = np.apply_along_axis(
            lambda x: len(_zerocrossings(x)), axis=1, arr=eeg_ep)

        # 5) Save features to dataframe
        features = pd.DataFrame(features)
        features.index.name = 'epoch'
        cols_eeg = features.filter(like="eeg_").columns.tolist()

        # 6) Apply centered rolling average / std (5 min 30)
        roll = features[cols_eeg].rolling(
            window=11, center=True, min_periods=1)
        feat_rollmean = roll.mean().add_suffix('_rollavg_c5min_norm')
        features = features.join(feat_rollmean)
        # feat_rollstd = roll.std(ddof=0).add_suffix('_rollstd_c5min_norm')
        # features = features.join(feat_rollstd)

        # 7) Apply in-place normalization on all "*_norm" columns
        features = features.join(features[cols_eeg].add_suffix("_norm"))
        cols_norm = features.filter(like="_norm").columns.tolist()
        cols_norm.remove('time_norm')  # make sure we remove 'time_norm'
        features[cols_norm] = robust_scale(
            features[cols_norm], quantile_range=(5, 95))

        # 8) Add metadata if present
        if self.metadata is not None:
            for c in self.metadata.keys():
                if c in ['age', 'male']:
                    features[c] = self.metadata[c]

        # 9) Add to self
        self.features = features

    def get_features(self, **kwargs):
        """Extract features from data and return a copy of the features dataframe.
        
        Parameters
        ----------
        kwargs : key, value mappings
            Keyword arguments are passed through to the fit method.

        Returns
        -------
        features : :py:class:`pandas.DataFrame`
            Feature dataframe.
        """
        if not hasattr(self, 'features'):
            self.fit(**kwargs)
        return self.features.copy()

    def predict(self, path_to_model):
        """
        Extract the features and predict the associated sleep stage with a user-specified 
        pre-trained classifier.
        """
        if not hasattr(self, 'features'):
            self.fit()
        # clf = joblib.load(path_to_model)

    def score(self, y_true, metric='accuracy'):
        """Validate automatic scoring against ground-truth scoring."""
        pass