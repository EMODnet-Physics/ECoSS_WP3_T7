# -*- coding: utf-8 -*-
"""
Created on Mon Jul 29 13:27:48 2024

@authors: Pablo Aguirre, Isabel Carozzo, Jose Antonio García, Mario Vilar
"""

""" This file contains the class EcossDataset, which is responsible for all the data generation
    and preprocessing steps. """

import pandas as pd
import os
from sklearn.model_selection import StratifiedShuffleSplit
from matplotlib import pyplot as plt
from dotenv import load_dotenv
import numpy as np
from enum import Enum
import librosa
import pickle
import os
import soundfile as sf
from pathlib import Path
import wave
from mutagen.flac import FLAC
from tqdm import tqdm
import logging

UNWANTED_LABELS = ["Undefined", "Waves", "Fishes", "MooringNoise", "Benthos", "Ship", "Chains"]
logging.basicConfig(level=logging.INFO)

logger = logging.getLogger(__name__)

handler = logging.FileHandler("log.log")
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)

class EcossDataset:
    def __init__(self, path_dataset: str, path_store_data: str, pad_mode: str,
                 sr: float, duration: float, saving_on_disk: bool):
        self.path_dataset = path_dataset
        self.path_store_data = path_store_data
        self.pad_mode = pad_mode
        self.sr = sr
        self.duration = duration
        self.segment_length = int(self.duration * self.sr)
        self.saving_on_disk = saving_on_disk
        self.path_annots = os.path.join(self.path_dataset, 'samples for training', 'annotations.csv')
        self.dataset_name = os.path.basename(os.path.normpath(self.path_dataset))
        self.df = pd.read_csv(self.path_annots, sep=";")
    
    @staticmethod
    def concatenate_ecossdataset(dataset_list):
        """
        Checks the EcossDataset object provided in a list have the same variables and then generates a new object with
        a concatenated dataframe. path_dataset and path_store_data taken from the first EcossDataset in the list.

        Inputs
        -------
        dataset_list : List with EcossDataset to be concatenated

        Outputs
        -------
        EcossDataset with concatenated DataFrame
        """

        #Extract values to compare
        path_dataset0 = dataset_list[0].path_dataset
        sr0 = dataset_list[0].sr
        duration0 = dataset_list[0].duration
        padding0 = dataset_list[0].pad_mode
        save0 = dataset_list[0].saving_on_disk
        path_store0 = dataset_list[0].path_store_data
        #Start populatinf DataFrame list
        df_list = [dataset_list[0].df]
        #Iterate over list to check appropiate values, exiting function it variables do not match
        for dataset in dataset_list[1:]:
            if dataset.sr != sr0 or dataset.duration != duration0 or dataset.pad_mode != padding0 or dataset.saving_on_disk != save0:
                logger.error("The datasets selected do not have the same characteristics")
                return
            else:
                df_list.append(dataset.df)
        #Create EcossDataset object with concatenated info
        ConcatenatedEcoss = EcossDataset(path_dataset=path_dataset0, path_store_data=path_store0,
                                         pad_mode=padding0, sr=sr0, duration=duration0, saving_on_disk=save0)
        ConcatenatedEcoss.df = pd.concat(df_list,ignore_index=True)
        return ConcatenatedEcoss


    def add_file_column(self):
        """
        Adds the file column in order to keep track of each file of the dataset

        Parameters:
       
        Nonce
 
        Returns:
        None (updates df atribute with an extra columnn named 'file')
        """
        self.df["file"] = ''
        for i, row in self.df.iterrows():
            self.df.at[i, "file"] = os.path.join(self.path_dataset,
                                                 'samples for training',
                                                 self.df.at[i, 'reference'])


    def filter_lower_sr(self):
        """
        Filters the rows of the df attribute which contains a sampling rate lower than the desired 

        Parameters:
       
        None
 
        Returns:
        None (updates pd.DataFrame: original DataFrame by filtering the signals with a lower sampling rate)
        """
        indexes_delete = []
        for i, row in self.df.iterrows():
            if os.path.isfile(row["file"]):
                if row["file"].endswith('.wav'):
                    with wave.open(row["file"], 'rb') as wav_file:
                        sr = wav_file.getframerate()
                        if sr < self.sr:
                            indexes_delete.append(i)
                            # print(f"Deleting file {row['file']} because it's sampling rate its {sr}")
                            logger.info(f"Deleting file {row['file']} because it's sampling rate its {sr}")
                elif row["file"].endswith('.flac'):
                    audio = FLAC(row["file"])
                    sr = audio.info.sample_rate
                    if sr < self.sr:
                        indexes_delete.append(i)
                        logger.info(f"Deleting file {row['file']} because it's sampling rate its {sr}")
                else:
                    raise ValueError("Unsupported file format. Only WAV and FLAC are supported.")
            else:
                indexes_delete.append(i)
                logger.info(f"File {row['file']} in the folder is missing")
        
        self.df.drop(indexes_delete, inplace=True)
        self.df.reset_index(drop=True, inplace=True)

    def split_train_test_balanced(self, test_size=0.2, random_state=None):
        """
        Divides the dataframe in train and test at file level, ensuring a balanced class distribution.
        Adds a 'split' column with values 'test' or 'train' 

        Parameters:
       
        test_size (float): Ratio of test dataset.
        random_state (int): Random seed.
 
        Returns:
        None (updates pd.DataFrame: original DataFrame with an extra columnn named 'split')
        """
        # Creates a DataFrame with unique files and their labels
        file_labels = self.df.groupby('parent_file')['final_source'].apply(
            lambda x: x.mode()[0]).reset_index()

        # Initialize column split in the original DataFrame
        self.df['split'] = ''

        # Handle classes with only one instance
        single_instance_files = file_labels[file_labels.duplicated('final_source', keep=False) == False]
        multiple_instance_files = file_labels[file_labels.duplicated('final_source', keep=False) == True]

        # Create StratifiedShuffleSplit for files with multiple instances
        sss = StratifiedShuffleSplit(n_splits=1, test_size=test_size, random_state=random_state)

        # Get indexes for train and test
        for train_idx, test_idx in sss.split(multiple_instance_files['parent_file'], multiple_instance_files['final_source']):
            train_files = multiple_instance_files['parent_file'].iloc[train_idx]
            test_files = multiple_instance_files['parent_file'].iloc[test_idx]
        # Add single instance files to the train set
        train_files = pd.concat([train_files, single_instance_files['parent_file']])

        # Assign in the original DataFrame 'train' or 'test' in columns 'split'
        self.df.loc[self.df['parent_file'].isin(train_files), 'split'] = 'train'
        self.df.loc[self.df['parent_file'].isin(test_files), 'split'] = 'test'
        
    def filter_overlapping(self, visualize_overlap = False):
        """
        Filters overlapping segments in the dataset and optionally generates a representation of the timeline of labels before and after filtering.

        Parameters:
        visualize_overlap (bool): If True, visualizes the timeline of labels before and after processing.

        Returns:
        None
        """
        overlap_info_processed = self._extract_overlapping_info()
        self.df["overlap_info_processed"] = overlap_info_processed
        # self.df.dropna(subset=["final_source"],inplace=True)
        self.df["to_delete"] = False
        if visualize_overlap:
            self._visualize_overlappping(self.df)
        # Iterate through the DataFrame to handle overlapping segments
        for eval_idx,_ in self.df.iterrows():
            not_count = False
            if not eval_idx in self.df.index:
                continue
            if np.isnan(self.df.loc[eval_idx]["overlapping"]):
                continue
            segments_to_delete = []
            for overlap_idx,tmin,tmax in self.df.loc[eval_idx]["overlap_info_processed"]:
                if overlap_idx not in self.df.index:
                    continue
                if self.df.loc[eval_idx]["final_source"] != self.df.loc[overlap_idx]["final_source"]:
                    # Add to segments_to_delete everytime there is overlapping different class sources
                    segments_to_delete.append([tmin,tmax])
                else:
                    # Handle when the two overlapping segments are from the same class
                    t_eval = [self.df.loc[eval_idx]['tmin'],self.df.loc[eval_idx]["tmax"]]
                    t_overlap = [self.df.loc[overlap_idx]['tmin'],self.df.loc[overlap_idx]["tmax"]]
                    
                    superpos = self._check_superposition(t_eval,t_overlap)
                    not_count = self._handle_superposition(eval_idx, overlap_idx, superpos)
                    if not_count:
                        break
            if not_count:
                continue
            # Divide the event into subevents excluding the segments_to_delete
            final_segments = self._divide_labels([self.df.loc[eval_idx]["tmin"],self.df.loc[eval_idx]["tmax"]],segments_to_delete)
            for tmin,tmax in final_segments:
                new_row = self.df.loc[eval_idx].copy()
                new_row["tmin"] = tmin
                new_row["tmax"] = tmax
                new_df = pd.DataFrame([new_row])
                new_df.index = [np.max(self.df.index)+1]
                self.df = pd.concat([self.df,new_df], axis=0)
            
            self.df.at[eval_idx,'to_delete'] = True
            
        # Remove rows marked for deletion
        self.df.drop(self.df[self.df["to_delete"]==True].index,inplace=True)
        self.df.drop(columns=['to_delete'], inplace=True)
        self.df.reset_index(drop=True, inplace=True)
        if visualize_overlap:
            self._visualize_overlappping(self.df,"_postprocessed") 
 
        
    def process_audios(self):
        pass
    
    
    def drop_unwanted_labels(self, unwanted_labels: list):
        """
        This function drops the rows that contain an unwanted label

        Parameters
        ----------
        unwanted_labels : list
            This list containing the unwanted labels.

        Returns
        -------
        None.

        """
        idxs_to_drop = []
        for i, row in self.df.iterrows():
            for label in unwanted_labels:
                if label in row["final_source"]:
                    idxs_to_drop.append(i)
        
        self.df = self.df.drop(idxs_to_drop)
        # self.df.reset_index(drop=True, inplace=True) Comment because its needed for the overlapping
        
    
    def fix_onthology(self, labels: list[str] = None):
        """
        This function generates a new column with the final labels on
        the annotations.csv file. If a list of labels is used, the function
        not only formats the labels to use the last part, but also fixes the
        onthology to the level of detail requested.
        
        Parameters
        ----------
        labels : list[str], optional
            A list with the labels to be modified.. The default is None.

        Returns
        -------
        None.

        """
        # Dropping rows that contain nan in label_source
        self.df = self.df.dropna(subset=['label_source'])
        # self.df.reset_index(drop=True, inplace=True) Comment because its needed for the overlapping
        
        if labels != None:
            for i, row in self.df.iterrows():
                for label in labels:
                    idx = row["label_source"].find(label)
                    if idx != -1:
                        delimiter = idx + len(label)
                        self.df.loc[i, "label_source"] = self.df.loc[i, "label_source"][:delimiter]
        
        # Once they are defined, we create the final column with the labels
        # Currently not saving, only overwritting the df parameter as this is the first step
        self.df["final_source"] = self.df["label_source"].apply(lambda x: x.split('|')[-1])
        
        # Now, we can proceed to eliminate the unwanted labels
        self.drop_unwanted_labels(UNWANTED_LABELS)
    
    def process_all_data(self):
        """
        Process the signals and return processed signals, labels, and splits according to the sample rate, duration, and pad_mode chosen.

        Returns:
        tuple: A tuple containing three lists:
            - processed_signals (list): List of processed signal segments.
            - processed_labels (list): List of labels corresponding to each processed segment.
            - processed_splits (list): List of split information ('train' or 'test') for each processed segment.
        """
        processed_signals = []
        processed_labels = []
        processed_splits = []
        files_dict = {}
        # TODO: Add to logging where are files being saved and info of the process 
        # Iterate over all signals,sr,paths,labels
        for i,row in tqdm(self.df.iterrows(), total=self.df.shape[0],desc='Processing Audios'):
            # [signal, original_sr, path, label]
            # Load audio file
            signal, original_sr = sf.read(row["file"])
            if "final_source" in row.index:
                label = row["final_source"]
            else:
                label = row["label_source"]
            split = row["split"]
            # Extract only the label segment
            signal = signal[int(original_sr*row["tmin"]):int(original_sr*row["tmax"])]
            # Process the signal
            segments = self.process_data(signal, original_sr)
            # Count how many times 
            if row["file"] in files_dict:
                files_dict[row["file"]] += 1
            else:
                files_dict[row["file"]] = 0
            
            path = Path(row["split"]) / label / f"{Path(row['file']).stem}_{files_dict[row['file']]:03d}"
            if self.saving_on_disk:
                try:
                    # Save the processed segments to disk
                    if self.saving_on_disk:
                        self.save_data(segments, path)
                except Exception as e:
                    logger.error('DataNotSaved', exc_info=True)
            # Extend the lists of processed signals and labels 
            processed_signals.extend(segments)
            processed_labels.extend([label] * len(segments))
            processed_splits.extend([split] * len(segments))
            
        
        # Ensure the lengths of signals and labels match  
        assert len(processed_signals)==len(processed_labels),f'Error : signals and labels processed have different length. Signal: {len(processed_signals)}, labels: {len(processed_labels)}'
        return processed_signals, processed_labels, processed_splits
                        
            
    def process_data(self, signal, original_sr):
        """
        Process a single signal by resampling and segmenting or padding it.

        Parameters:
        signal (np.array): Signal array.
        original_sr (float): Original sampling rate of the signal.

        Returns:
        list: List of processed segments.
        """
        # Resample the signal if the original sampling rate is different from the target
        if original_sr != self.sr:
            signal = librosa.resample(y=signal, orig_sr=original_sr, target_sr=self.sr)
         
        # Pad the signal if it is shorter than the segment length   
        if len(signal) < self.segment_length:
            segments = self.make_padding(signal)
         # Segment the signal if it is longer or equal to the segment length
        elif len(signal) >= self.segment_length:
            segments = self.make_segments(signal)
            
        return segments
    
    
    def make_segments(self, signal):
        """
        Segment a signal into equal parts based on segment length (duration).

        Parameters:
        signal (np.array): Signal array.

        Returns:
        list: List of signal segments.
        """
        segments = []
        # Calculate the number of full segments in the signal
        n_segments = len(signal)//(self.segment_length)
        # Extract each segment and append to the list
        for i in range(n_segments):
            segment = signal[(i*self.segment_length):((i+1)*self.segment_length)]
            segments.append(segment)
        return segments
            
            
    def make_padding(self, signal):
        """
        Pad a signal to match a fixed segment length using the specified pad mode.

        Parameters:
        signal (np.array): Signal array.
        Returns:
        list: List containing the padded signal.
        """
        # Calculate the amount of padding needed
        delta = self.segment_length - len(signal)
        delta_start = delta // 2
        delta_end = delta_start if delta%2 == 0 else (delta // 2) + 1 
        
        # Pad the signal according to the specified mode
        if self.pad_mode == 'zeros':
           segment = self.zero_padding(signal, delta_start, delta_end)
        elif self.pad_mode == 'white_noise':
            segment = self.white_noise_padding(signal, delta_start, delta_end)
        else:
            logger.error("Error : pad_mode not valid")
            exit(1)
            
        return [segment]
    

    def zero_padding(self, signal, delta_start, delta_end):
        """
        Pad the signal with zeros.

        Parameters:
        signal (np.array): Signal array.
        delta_start (int): Number of zeros to add at the start.
        delta_end (int): Number of zeros to add at the end.

        Returns:
        np.array: Zero-padded signal.
        """
        segment = np.pad(signal, (delta_start, delta_end), 'constant', constant_values=(0, 0))
        
        return segment
    
    
    def white_noise_padding(self, signal, delta_start, delta_end):
        """
        Pad the signal with white noise.

        Parameters:
        signal (np.array): Signal array.
        delta_start (int): Number of padding values to add at the start.
        delta_end (int): Number of padding values to add at the end.

        Returns:
        np.array: White-noise padded signal.
        """
        # TODO we should decide is std needs to be a parameter to set or not 
        # Generate white noise with standard deviation scaled to the signal
        std = np.std(signal)/10
        white_noise_start = np.random.normal(loc=0, scale=std, size=delta_start)
        white_noise_end = np.random.normal(loc=0, scale=std, size=delta_end)

        # Concatenate white noise segments with the original signal
        segment = np.concatenate((white_noise_start, signal, white_noise_end))

        return segment
    
    
    def save_data(self, segments, path):
        """
        Save the processed segments to disk in the specified format (pickle or wav).

        Parameters:
        segments (list): List of processed segments to be saved.
        path (str): Path to the directory where the segments will be saved.

        Raises:
        ValueError: If the saving format specified in self.saving_on_disk is not 'pickle' or 'wav'.

        Notes:
        - If the saving format is 'pickle', each segment will be saved as a separate pickle file.
        - If the saving format is 'wav', each segment will be saved as a separate wave file.
        - The files will be saved in the directory specified by self.path_store_data combined with the provided path.
        - The directory will be created if it does not exist.
        """
        # Create the cache directory if it does not exist
        save_path = Path(self.path_store_data) / path
        save_path.parent.mkdir(parents = True, exist_ok = True)
        filename = save_path 
        if self.saving_on_disk == "pickle":
            # Save each segment as a separate pickle file
            for idx, segment in enumerate(segments):
                saving_filename = str(filename) + '-' + f"{idx:03d}" + '.pickle'
                with open(saving_filename, 'wb') as f:
                    pickle.dump(segment, f, protocol=pickle.HIGHEST_PROTOCOL)
        elif self.saving_on_disk == "wav":
            # Save each segment as a separate wave file
            for idx, segment in enumerate(segments):
                saving_filename = str(filename) + '-' + f"{idx:03d}" + '.wav'
                sf.write(saving_filename, segment, int(self.sr))
        else:
            raise ValueError(f"saving_on_disk should be pickle or wav, not {self.saving_on_disk}")
        
    # TODO: When the splitting is performed, go for metrics per split
    def generate_insights(self):
        """
        This function is used to generate insights on the data. It generates plots
        for the number of sound signatures per class, and the time per class.
        
        IMPORTANT: It needs to be used right after the first step (the remapping and reformating of classes).
        However, you dont need to remap the labels if you don't want to, as this parameter
        is optional.

        Returns
        -------
        None.

        """
        # Plot for number of sound signatures (time independent)
        count_signatures = self.df["final_source"].value_counts()
        plt.figure(figsize=(8,6))
        plt.bar(range(0, len(count_signatures)), count_signatures)
        plt.xticks(range(0, len(count_signatures)),
                   count_signatures.index.to_list(),
                   horizontalalignment='center')
        plt.xlabel("Source")
        plt.ylabel("# of sound signatures")
        plt.show()
        
        logger.info(f"Number of sound signatures per source: {count_signatures}\n")

        # Plot for time per class of sound signature
        times = dict()
        for i, row in self.df.iterrows():
            if row["final_source"] not in times.keys():
                times[row["final_source"]] = row["tmax"] - row["tmin"]
            else:
                times[row["final_source"]] += row["tmax"] - row["tmin"]
        plt.figure(figsize=(8,6))
        plt.bar(range(0, len(times)), times.values())
        plt.xticks(range(0, len(times)),
                   list(times.keys()),
                   horizontalalignment='center')
        plt.xlabel("Source")
        plt.ylabel("Time (s)")
        plt.show()

        logger.info(f"Number of seconds per source: {times}\n")

        # Plotting per split (train and test)
        if 'split' in self.df.columns:
            df_train = self.df[self.df["split"] == "train"]
            df_test = self.df[self.df["split"] == "test"]
            
            # Number of sound signatures related
            fig, ax = plt.subplots(ncols=2, figsize=(12,6))
            count_signatures_train = df_train["final_source"].value_counts()
            count_signatures_test = df_test["final_source"].value_counts()
            ax[0].bar(range(0, len(count_signatures_train)), count_signatures_train)
            ax[1].bar(range(0, len(count_signatures_test)), count_signatures_test)

            ax[0].set_xticks(range(0, len(count_signatures_train)),
                               count_signatures_train.index.to_list(),
                               horizontalalignment='center',
                               rotation=45)
            ax[1].set_xticks(range(0, len(count_signatures_test)),
                               count_signatures_test.index.to_list(),
                               horizontalalignment='center',
                               rotation=45)

            ax[0].set_xlabel("Source")
            ax[0].set_ylabel("# of sound signatures")
            
            ax[0].set_title("Train data")
            ax[1].set_title("Test data")
            

            logger.info(f"Number of sound signatures per source for train set: {count_signatures_train}\n")
            logger.info(f"Number of sound signatures per source for test set: {count_signatures_test}\n")
            
            plt.tight_layout()
            plt.show()
            
            # Time related
            times_train = dict()
            times_test = dict()

            for i, row in df_train.iterrows():
                if row["final_source"] not in times_train.keys():
                    times_train[row["final_source"]] = row["tmax"] - row["tmin"]
                else:
                    times_train[row["final_source"]] += row["tmax"] - row["tmin"]
            for i, row in df_test.iterrows():
                if row["final_source"] not in times_test.keys():
                    times_test[row["final_source"]] = row["tmax"] - row["tmin"]
                else:
                    times_test[row["final_source"]] += row["tmax"] - row["tmin"]

            fig, ax = plt.subplots(ncols=2, figsize=(12,6))
            ax[0].bar(range(0, len(times_train)), times_train.values())
            ax[1].bar(range(0, len(times_test)), times_test.values())
            
            ax[0].set_xticks(range(0, len(times_train)),
                               list(times_train.keys()),
                               horizontalalignment='center',
                               rotation=45)
            ax[1].set_xticks(range(0, len(times_test)),
                               list(times_test.keys()),
                               horizontalalignment='center',
                               rotation=45)

            ax[0].set_xlabel("Source")
            ax[0].set_ylabel("Time (s)")
            
            ax[0].set_title("Train data")
            ax[1].set_title("Test data")


            logger.info(f"Number of seconds per source for train set: {times_train}\n")
            logger.info(f"Number of seconds per source for test set: {times_test}\n")

            plt.tight_layout()
            plt.show()
            

    def _extract_overlapping_info(self):
        """
        Extracts and processes overlapping information from the DataFrame.

        Returns:
        list: A list of processed overlapping information for each row in the DataFrame.
        """
        overlap_info_processed = []
        # Process each row to handle overlapping information
        for eval_idx,row in self.df.iterrows():
            if pd.isna(row["overlapping"]):
                overlap_info_processed.append([])
                continue
            overlap_info_processed.append(self._parse_overlapping_field(row["overlap_info"]))
        return overlap_info_processed   
    
    def _visualize_overlappping(self,df,append = ""):
        """
        Visualizes overlapping segments for each unique parent file in the DataFrame.

        Parameters:
        df (pd.DataFrame): The DataFrame containing the segments to visualize.
        append (str): A suffix to append to the visualization filename.

        Returns:
        None
        """
        # Get unique parent files
        files = df["parent_file"].unique()
        
        for file in files:
            segments = []
            labels = []
            
            # Iterate through rows corresponding to the current parent file
            for eval_idx, row in df[df["parent_file"] == file].iterrows():
                segments.append([row['tmin'], row["tmax"]])
                labels.append(row['final_source'])
            
            # Plot segments for the current file
            self._plot_segments(segments, labels, file, append=append)
    
    @staticmethod
    def _plot_segments(segments, labels, filename, append=""):
        """
        Plots the segments with their corresponding labels and saves the plot as a PNG file.

        Parameters:
        segments (list of lists): A list of [t_min, t_max] pairs representing the segments.
        labels (list): A list of labels corresponding to each segment.
        filename (str): The base filename for the saved plot.
        append (str): A suffix to append to the filename.

        Returns:
        None
        """
        try:
            fig, ax = plt.subplots()
            labels_unique = list(np.unique(labels))
            # Plot each segment
            for i, (t_min, t_max) in enumerate(segments):
                ax.plot([t_min, t_max], [labels_unique.index(labels[i]), labels_unique.index(labels[i])], marker='o')
            ax.set_yticks(range(len(labels_unique)))
            ax.set_yticklabels([f'{x}' for x in labels_unique])
            ax.set_xlabel('Time')
            ax.set_title(f'{filename}')
            ax.set_xlim([0, np.max(segments) + 10])
            # Save the plot
            plt.savefig(filename + append + ".png")
        except Exception as e:
            logger.error('ErrorPlotting', exc_info=True)
        finally:
            plt.close(fig) 
    @staticmethod
    def _parse_overlapping_field(overlapping_str):
        """
        Parses a string containing overlapping segment information and returns a list of tuples.

        Parameters:
        overlapping_str (str): A string containing overlapping segment information.

        Returns:
        list of tuples: A list where each tuple contains (index, start, stop) for each overlapping segment.
        """
        # Split the string by commas and remove the last empty element
        overlapping_str = overlapping_str.split(",")[:-1]
        
        # Group the elements into sublists of three elements each
        divided_list = [overlapping_str[i:i + 3] for i in range(0, len(overlapping_str), 3)]
        
        overlap_info = []
        
        # Parse each sublist to extract index, start, and stop values
        for x in divided_list:
            index = [int(s) for s in x[0].split() if s.isdigit()][0]
            start = float(x[1].split(':')[-1])
            stop = float(x[2].split(':')[-1])
            overlap_info.append((index, start, stop))
        
        return overlap_info
    @staticmethod
    def _check_superposition(segment1, segment2):
        """
        Checks the superposition between two time intervals.

        Parameters:
        segment1 (list): The time interval of the first segment.
        segment2 (list): The time interval of the second segment.

        Returns:
        SuperpositionType: An enum indicating the type of superposition.
        """
        t_min1, t_max1 = segment1
        t_min2, t_max2 = segment2

        if t_min1 <= t_min2 <= t_max1 < t_max2:
            return SuperpositionType.STARTS_BEFORE_AND_OVERLAPS
        elif t_min2 <= t_min1 <= t_max2 < t_max1:
            return SuperpositionType.STARTS_AFTER_AND_OVERLAPS
        elif t_min1 <= t_min2 and t_max1 >= t_max2:
            return SuperpositionType.CONTAINS
        elif t_min2 <= t_min1 and t_max2 >= t_max1:
            return SuperpositionType.IS_CONTAINED
        else:
            return SuperpositionType.NO_SUPERPOSITION

    @staticmethod
    def _divide_labels(event, segments):
        """
        Divides an event into subevents by excluding specified segments.

        Parameters:
        event (list): A list containing the start and end times of the event [tmin, tmax].
        segments (list of lists): A list of [tmin, tmax] pairs representing the segments to exclude.

        Returns:
        list of lists: A list of [tmin, tmax] pairs representing the subevents.
        """
        tmin, tmax = event
        subevents = []
        start = tmin

        # Sort the segments by their start time
        segments.sort()

        for segment in segments:
            if segment[0] > start:
                subevents.append([start, segment[0]])
            # Update the start to the maximum between the end of the current segment and the current start
            start = max(start, segment[1])

        if start < tmax:
            subevents.append([start, tmax])

        return subevents
    
    def _handle_superposition(self, eval_idx, overlap_idx, superpos):
        """
        Handles the superposition between two segments.

        Parameters:
        eval_idx (int): The index of the evaluated segment.
        overlap_idx (int): The index of the overlapping segment.
        superpos (SuperpositionType): The type of superposition.

        Returns:
        bool: True if the evaluated segment should not be counted, False otherwise.
        """
        if superpos == SuperpositionType.STARTS_BEFORE_AND_OVERLAPS:
            self.df.at[eval_idx, 'tmax'] = self.df.loc[overlap_idx]["tmin"]
            self.df.at[overlap_idx, 'tmin'] = self.df.loc[eval_idx]["tmax"]
            return False
        elif superpos == SuperpositionType.STARTS_AFTER_AND_OVERLAPS:
            self.df.at[eval_idx, 'tmin'] = self.df.loc[overlap_idx]["tmax"]
            self.df.at[overlap_idx, 'tmax'] = self.df.loc[eval_idx]["tmin"]
            return False
        elif superpos == SuperpositionType.IS_CONTAINED:
            self.df.at[eval_idx, 'to_delete'] = True
            return True
        else:
            return False



class SuperpositionType(Enum):
    NO_SUPERPOSITION = 0
    STARTS_BEFORE_AND_OVERLAPS = 1
    STARTS_AFTER_AND_OVERLAPS = 2
    CONTAINS = 3
    IS_CONTAINED = 4        

if __name__ == "__main__":
    load_dotenv()
    ANNOTATIONS_PATH = os.getenv("DATASET_PATH")
    ANNOTATIONS_PATH2 = os.getenv("DATASET_PATH2")
    ANNOTATIONS_PATH3 = os.getenv("DATASET_PATH3")
    # LABELS =
    ecoss_list = []
    for ANNOT_PATH in [ANNOTATIONS_PATH, ANNOTATIONS_PATH2, ANNOTATIONS_PATH3]:
        ecoss_data1 = EcossDataset(ANNOT_PATH, 'data/', 'zeros', 32000.0, 1,"wav")
        ecoss_data1.add_file_column()
        ecoss_data1.fix_onthology(labels=[])
        ecoss_data1.filter_overlapping()
        ecoss_list.append(ecoss_data1)
        
    ecoss_data = EcossDataset.concatenate_ecossdataset(ecoss_list)
    length_prior_filter = len(ecoss_data.df)
    ecoss_data.filter_lower_sr()
    times = ecoss_data.generate_insights()
    ecoss_data.split_train_test_balanced(test_size=0.3, random_state=27)

    
    signals_processed, labels_processed,split  = ecoss_data.process_all_data()
