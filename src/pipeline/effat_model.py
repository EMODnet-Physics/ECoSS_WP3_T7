# -*- coding: utf-8 -*-
"""
Created on Fri Aug 02 10:10:48 2024

@authors: Pablo Aguirre, Isabel Carozzo, Jose Antonio García, Mario Vilar
"""

""" This script implements the class EffAtModel which is responsible for all the training, testing and inference stuff related with the
    EfficientAT model """

import yaml
import logging
from pathlib import Path
import os
import numpy as np
from matplotlib import pyplot as plt
import torchaudio
from tqdm import tqdm
from torch.utils.data import Dataset, DataLoader, WeightedRandomSampler
import torch.nn as nn
import torch.optim as optim
import torch
import json
from sklearn.metrics import f1_score, confusion_matrix
from glob import glob
import seaborn as sns
import pandas as pd
from typing import Tuple, Dict, Union

from .utils import AugmentMelSTFT, EffATWrapper, process_audio_for_inference
from models.effat_repo.models.mn.model import get_model as get_mn
from models.effat_repo.models.dymn.model import get_model as get_dymn

logging.basicConfig(level=logging.INFO)

logger = logging.getLogger(__name__)

handler = logging.FileHandler("log.log")
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)

class HelperDataset(Dataset):
    def __init__(self, path_data: str, sr: float, duration: float,
                 mel, train: bool = True, label_to_idx: dict = None):
        """The constructor for the HelperDataset class. A class used to feed the data generated by EcossDataset class into EffAtModel class.

        Args:
            path_data (str): Path to the folder where train and tets folder are located
            sr (float): The sampling rate of the generated dataset
            duration (float): The duration of the generated dataset clips
            mel (AugmentMelSTFT): The AugmentMelSTFT instance
            train (bool, optional): If True, it loads the data inside the train folder, if False, loads the test folder. Defaults to True.
            label_to_idx (dict, optional): Dictionary that associated a class to a integer. Defaults to None.
        """
        self.train = train
        if self.train == True:
            self.path_data = os.path.join(path_data, 'train')
        else:
            self.path_data = os.path.join(path_data, 'test')

        self.sr = sr
        self.duration = duration
        self.mel = mel
        self.classes = os.listdir(self.path_data)
        data, labels = [], []

        if not label_to_idx:
            self.label_to_idx = {cls: i for i, cls in enumerate(self.classes)}
        else:
            self.label_to_idx = label_to_idx

        for cls in self.classes:
            files = [os.path.join(self.path_data, cls, file) for file in os.listdir(os.path.join(self.path_data, cls))]
            for file in files:
                data.append((file, self.label_to_idx[cls]))
                labels.append(self.label_to_idx[cls])
        
        self.data = data
        self.labels = labels

    def __len__(self):
        return len(self.data)


    def __getitem__(self, index):
        path_audio, label = self.data[index]
        y, _ = torchaudio.load(path_audio)
        return self.mel(y), label, path_audio


class EffAtModel():
    def __init__(self, yaml_content: dict, data_path: str, num_classes: int) -> None:
        """The constructor for the EffAtModel class. A class responsible for all tasks related to the EfficientAT model.

        Args:
            yaml_content (dict): The content after reading the config.yaml
            data_path (str): The path where the train and test folders are located
            num_classes (int): The number of classes that the model will have to predict among
        """
        self.yaml = yaml_content
        self.data_path = data_path
        self.mel = AugmentMelSTFT(freqm=self.yaml["freqm"],
                                  timem=self.yaml["freqm"],
                                  n_mels=self.yaml["n_mels"],
                                  sr=self.yaml["sr"],
                                  win_length=self.yaml["win_length"],
                                  hopsize=self.yaml["hopsize"],
                                  n_fft=self.yaml["n_fft"],
                                  fmin=self.yaml["fmin"],
                                  fmax=self.yaml["fmax"],
                                  fmax_aug_range=self.yaml["fmax_aug_range"],
                                  fmin_aug_range=self.yaml["fmin_aug_range"])
        self.name_model = self.yaml["model_name"]
        self.num_classes = num_classes
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        logger.info(f"The device used for training is {self.device}")

        if "dy" not in self.name_model:
            model = get_mn(pretrained_name=self.name_model)
        else:
            model = get_dymn(pretrained_name=self.name_model)

        # Using the wrapper to modify the last layer and moving to device
        model = EffATWrapper(num_classes=num_classes, model=model, freeze=self.yaml["freeze"])
        model.to(self.device)
        if self.yaml["compile"]:
            model = torch.compile(model,mode = "reduce-overhead")
        
        self.model = model


    def load_aux_datasets(self) -> Tuple[DataLoader, DataLoader, Dict[str, int]]:
        """Function that uses the HelperDataset class in order to generate the pytorch dataloaders to model the data.
        It applies WeightedRandomSampler on the train_dataloader to prevent overfitting due to unbalaced classes.

        Returns:
            train_dataloader (torch.utils.data.DataLoader):  DataLoader for the training dataset, with weighted sampling applied.
            test_dataloader (torch.utils.data.DataLoader): DataLoader for the testing dataset, without weighted sampling.
            label_to_idx (dict): A dictionary mapping each label in the training dataset to its corresponding index.
        """
        dataset_train = HelperDataset(path_data = self.data_path, sr=self.yaml["sr"],
                                      duration=self.yaml["duration"], mel=self.mel,
                                      train=True,
                                      label_to_idx=None)
        logger.debug("Training dataset obtained")

        dataset_test = HelperDataset(path_data = self.data_path, sr=self.yaml["sr"],
                                     duration=self.yaml["duration"], mel=self.mel,
                                     train=False,
                                     label_to_idx=dataset_train.label_to_idx)
        logger.debug("Testing dataset obtained")

        # Create the WeightedRandomSampler for unbalanced datasets
        train_labels = dataset_train.labels
        logger.debug("Training labels obtained")
        class_counts = np.bincount(train_labels)
        logger.debug("Class counts obtained")
        class_weights = 1. / class_counts
        samples_weights = class_weights[train_labels]
        samples_weights = torch.FloatTensor(samples_weights)
        class_weights = torch.FloatTensor(class_weights).to(self.device)

        logger.debug("Everything set for the WeightedRandomSampler")

        train_sampler = WeightedRandomSampler(weights=samples_weights, num_samples=len(samples_weights), replacement=True)
        logger.debug("WRS obtained")

        train_dataloader = DataLoader(dataset=dataset_train, sampler=train_sampler, batch_size=self.yaml["batch_size"])
        test_dataloader = DataLoader(dataset=dataset_test, batch_size=self.yaml["batch_size"])  # Not doing weighted samples for testing
        logger.debug("DLs obtained")

        return train_dataloader, test_dataloader, dataset_train.label_to_idx


    def train(self, results_folder: str) -> None:
        """Trains the model and save everything into the specified folder

        Args:
            results_folder (str): The path to the folder where all the results will be stored.
        """
        # Saving the configuration.yaml inside the results folder
        self.results_folder = Path(results_folder)
        logging.info(f"Training EffAT")
        output_config_path = self.results_folder / 'configuration.yaml'
        logging.info(f"Saving configuration in {output_config_path}")
        with open(str(output_config_path), 'w') as outfile:
            yaml.dump(self.yaml, outfile, default_flow_style=False)
        logging.info(f"Config params:\n {self.yaml}")

        # Begin the training
        self.model.train()
        logging.info("Model set to train mode")
        if self.yaml["optimizer"].lower() == "adam":
            optimizer = optim.Adam(self.model.parameters(), lr=self.yaml["lr"])
        else:
            optimizer = optim.SGD(self.model.parameters(), lr=self.yaml["lr"])

        criterion = nn.CrossEntropyLoss()
        logging.info("Criterion and optimizer selected")
        best_accuracy = 0.0
        epochs_without_improvement = 0

        train_accs, test_accs = [], []
        train_losses, test_losses = [], []

        train_dataloader, test_dataloader, label_encoder = self.load_aux_datasets()
        logging.info("Dataloaders obtained")
        for i in tqdm(range(self.yaml["n_epochs"]), desc="Epoch"):
            self.model.train()
            running_loss = 0.0
            batch_count = 0
            correct = 0
            total = 0
            all_preds = []
            all_labels = []

            for inputs, labels, _ in tqdm(train_dataloader, desc="Train"):
                inputs, labels = inputs.to(self.device), labels.to(self.device)
                optimizer.zero_grad()

                # Forward pass
                outputs, _ = self.model(inputs)
                outputs = outputs.squeeze()

                loss = criterion(outputs, labels)

                # Backward pass and optimization
                loss.backward()
                optimizer.step()

                running_loss += loss.item()
                batch_count += 1

                 # Calculate training accuracy
                _, predicted = torch.max(outputs, 1)
                total += labels.size(0)
                correct += (predicted == labels).sum().item()
                all_preds.extend(predicted.cpu().numpy())
                all_labels.extend(labels.cpu().numpy())

            epoch_loss = running_loss / batch_count
            train_accuracy = 100 * correct / total
            train_f1 = f1_score(all_labels, all_preds, average='macro')

            # Evaluation
            self.model.eval()
            test_loss = 0.0
            batch_count = 0
            correct = 0
            total = 0
            all_preds = []
            all_labels = []

            with torch.no_grad():
                for inputs, labels, _ in tqdm(test_dataloader, desc="Test"):
                    inputs, labels = inputs.to(self.device), labels.to(self.device)
                    outputs, _ = self.model(inputs)
                    outputs = outputs.squeeze()

                    loss = criterion(outputs, labels)
                    test_loss += loss.item()
                    batch_count += 1

                    _, predicted = torch.max(outputs, 1)
                    total += labels.size(0)

                    correct += (predicted == labels).sum().item()
                    all_preds.extend(predicted.cpu().numpy())
                    all_labels.extend(labels.cpu().numpy())


            avg_test_loss = test_loss / batch_count
            test_accuracy = 100 * correct / total
            test_f1 = f1_score(all_labels, all_preds, average='macro')

            train_losses.append(epoch_loss)
            test_losses.append(avg_test_loss)
            train_accs.append(train_accuracy)
            test_accs.append(test_accuracy)

            logging.info(f"Epoch {i}: Train loss -> {epoch_loss}, test loss -> {avg_test_loss}, train accuracy -> {train_accuracy}, test accuracy -> {test_accuracy}")

            if test_accuracy > best_accuracy:
                best_accuracy = test_accuracy
                epochs_without_improvement = 0  # Reset counter if we see improvement
                logging.info(f"New best testing accuracy: {best_accuracy}")

                # Compute the confusion matrix in the testing dataset (each time it saves another better model)
                cm = confusion_matrix(all_labels, all_preds)

                # Saving weights, results and curves
                self.plot_results(train_losses, test_losses, train_accs, test_accs)
                self.plot_cm(cm)
                self.save_weights(optimizer)
                metrics = {"train_acc": train_accuracy,
                           "test_acc": test_accuracy,
                           "train_f1": train_f1,
                           "test_f1": test_f1}

                self.save_results(label_encoder, metrics)

            else:
                epochs_without_improvement += 1
                logging.info(f"No improvement for {epochs_without_improvement} epoch(s).")

            if epochs_without_improvement >= self.yaml["patience"]:
                logging.info(f"Early stopping triggered after {i+1} epochs.")
                break


    def test(self, results_folder: str, path_model: str, path_data: str) -> None:
        """Function used to test a trained model on a generated dataset (train or test folder)

        Args:
            results_folder (str): The path where the results will be stored
            path_model (str): The path to the weights that want to be loaded inside the model
            path_data (str): The path where the train and test folders are located
        """
        self.results_folder = Path(results_folder)
        self.test_data_path = path_data

        # Load the weights
        checkpoint = torch.load(path_model)
        model_state_dict = checkpoint['model_state_dict']
        if not self.yaml["compile"]:
            remove_prefix = '_orig_mod.'
            model_state_dict = {k[len(remove_prefix):] if k.startswith(
                    remove_prefix) else k: v for k, v in model_state_dict.items()}
        self.model.load_state_dict(model_state_dict)
        
        self.model.eval()
        self.mel.eval()
        logger.info(f"Weights succesfully loaded into the model")

        # Get the mapping
        class_map_path = path_model.replace('model.pth', 'class_dict.json')
        with open(class_map_path, 'r') as f:
            class_map = json.load(f)

        logger.info(f"The class mapping that will be used is {class_map}")

        # Prepare the dataset
        test_dataset = HelperDataset(path_data=path_data, sr=self.yaml["sr"],
                                     duration=self.yaml["duration"], mel=self.mel, train=self.yaml["test_on_train"], label_to_idx=class_map)
        test_dataloader = DataLoader(dataset=test_dataset, batch_size=self.yaml["batch_size"])

        logger.info("Dataset succesfully generated")

        correct = 0
        total = 0
        all_preds = []
        all_labels = []

        with torch.no_grad():
            for inputs, labels, _ in tqdm(test_dataloader, desc="Test"):
                inputs, labels = inputs.to(self.device), labels.to(self.device)
                outputs, _ = self.model(inputs)
                outputs = outputs.squeeze()

                _, predicted = torch.max(outputs, 1)
                total += labels.size(0)
                correct += (predicted == labels).sum().item()
                all_preds.extend(predicted.cpu().numpy())
                all_labels.extend(labels.cpu().numpy())

        test_accuracy = 100 * correct / total
        test_f1 = f1_score(all_labels, all_preds, average='macro')

        metrics = {"test_acc": test_accuracy,
                   "test_f1": test_f1}

        self.save_results(class_map, metrics)
        cm = confusion_matrix(all_labels, all_preds)
        self.plot_cm(cm)


    def inference(self, results_folder: str, path_model: str, path_data: str) -> None:
        """Performs inference on a file

        Args:
            results_folder (str): The folder where the results of the inference will be performed
            path_model (str): The path to the weights of the model to be used
            path_data (str): The path to the inference_set
        """
        self.results_folder = Path(results_folder)
        # Load the model
        checkpoint = torch.load(path_model)
        # Load the state dicts into the model
        model_state_dict = checkpoint['model_state_dict']
        if not self.yaml["compile"]:
            remove_prefix = '_orig_mod.'
            model_state_dict = {k[len(remove_prefix):] if k.startswith(
                    remove_prefix) else k: v for k, v in model_state_dict.items()}
        self.model.load_state_dict(model_state_dict)
        
        self.model.eval()
        self.mel.eval()
        # Obtain the class mapping
        class_map_path = path_model.replace('model.pth', 'class_dict.json')
        with open(class_map_path, 'r') as f:
            class_map = json.load(f)
        inverse_class_map = {v: k for k, v in class_map.items()}

        outs, embs = [], []
        preds = {}
        with torch.no_grad():
            y, _ = process_audio_for_inference(path_audio=path_data,
                                                desired_sr=self.yaml["sr"],
                                                desired_duration=self.yaml["duration"])
        
            for i in tqdm(range(y.shape[1])):
                output, embeddings = self.model(self.mel(y[:, i]).unsqueeze(0).to(self.device))  # Saving embeddings but not necessary
                outs.append(output)
                softmax = nn.Softmax(dim=1)
                percentages = softmax(output)
                predictions = torch.argmax(percentages).item()
                preds[f"chunk_{i}"] = {
                    'Predicted Class': inverse_class_map[predictions],
                    'Confidence per class': {k: float(percentages.cpu().numpy()[0, idx]) for idx, k in enumerate(class_map.keys())}
                }
                embs.append(embeddings)

        with open(self.results_folder / 'predictions.json', "w") as f:
            json.dump(preds, f)


    def plot_results(self, train_loss: list, test_loss: list, train_acc: list, test_acc: list) -> None:
        """This function is used to plot and save the figures of the training process

        Args:
            train_loss (list): A list containing all the training losses
            test_loss (list): A list containing all the training losses
            train_acc (list): A list containing all the training losses
            test_acc (list): A list containing all the training losses
        """
        plt.figure()
        plt.plot(train_loss, label="Train losses")
        plt.plot(test_loss, label="Test losses")
        plt.legend()
        plt.savefig(self.results_folder / 'losses.png')
        plt.close()

        plt.figure()
        plt.plot(train_acc, label="Train accuracy")
        plt.plot(test_acc, label="Test accuracy")
        plt.legend()
        plt.savefig(self.results_folder / 'accuracies.png')
        plt.close()


    def plot_cm(self, cm: np.ndarray) -> None:
        """Function to plot the confusion matrix

        Args:
            cm (np.ndarray): The sklearn confusion matrix, a ndarray of shape (n_classes, n_classes)
        """
        plt.figure()
        sns.heatmap(cm, annot=True, cmap='Blues', fmt='d')
        plt.ylabel("True")
        plt.xlabel("Predicted")
        plt.title("val_result - Confusion Matrix")
        plt.savefig(self.results_folder / 'confusion_matrix.png')
        plt.close()


    def save_weights(self, optimizer: Union[optim.Adam, optim.SGD]) -> None:
        """It is used to save the state dict of the model as well as the optimizer (in case we want to retrain)

        Args:
            optimizer (Union[optim.Adam, optim.SGD]): The optimizer used for the train process.
        """
        torch.save({
                'model_state_dict': self.model.state_dict(),
                'optimizer_state_dict': optimizer.state_dict()
                }, self.results_folder / 'model.pth')


    def save_results(self, label_encoder: dict, metrics: dict) -> None:
        """It generates the class_dict.json and the metrics.json files and saves it on the results_folder parameter

        Args:
            label_encoder (dict): Contains the mapping of the classes
            metrics (dict): Contains the metrics to be saved
        """
        # Save the class dictionary
        with open(self.results_folder / 'class_dict.json', 'w') as json_file:
            json.dump(label_encoder, json_file)

        # Save the results
        with open(self.results_folder / 'metrics.json', 'w') as json_file:
            json.dump(metrics, json_file)


    def plot_processed_data(self, augment: bool = True) -> None:
        """This function will plot a random mel spectrogram per class available for the training


        Args:
            augment (bool, optional): If se to true, the mel will be augmented. Defaults to True.
        """
        path_classes = os.path.join(self.data_path, "train")
        available_classes = os.listdir(path_classes)

        if augment == False:
            self.mel.eval()

        for av_class in available_classes:
            path_wavs = os.path.join(path_classes, av_class)
            wav_to_plot = os.path.join(path_wavs,
                                       np.random.choice(os.listdir(path_wavs)))
            logger.info(f"The file that will be plotted is {wav_to_plot}")

            y, sr = torchaudio.load(wav_to_plot)
            melspec = self.mel(y)
            logger.info(f"The shape of the melspec is {melspec.shape}")

            plt.figure()
            plt.imshow(melspec[0], origin="lower")
            plt.title(av_class)
            plt.show()




