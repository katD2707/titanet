import torch
import torch.nn as nn
import torch.nn.functional as F

import modules


def get_titanet(n_mels=80, n_mega_blocks=5, model_size="s"):
    """
    Return one of the three TitaNet instances described in the paper,
    i.e. TitaNet-S, TitaNet-M or TitaNet-L
    """
    assert isinstance(model_size, str) and model_size.lower() in (
        "s",
        "m",
        "l",
    ), "Unsupported model size"
    if model_size.lower() == "s":
        return TitaNet(
            n_mels,
            n_mega_blocks,
            n_sub_blocks=3,
            encoder_hidden_size=256,
            encoder_output_size=1536,
            embedding_size=192,
            mega_block_kernel_size=3,
        )
    elif model_size.lower() == "m":
        return TitaNet(
            n_mels,
            n_mega_blocks,
            n_sub_blocks=3,
            encoder_hidden_size=512,
            encoder_output_size=1536,
            embedding_size=192,
            mega_block_kernel_size=7,
        )
    elif model_size.lower() == "l":
        return TitaNet(
            n_mels,
            n_mega_blocks,
            n_sub_blocks=3,
            encoder_hidden_size=1024,
            encoder_output_size=1536,
            embedding_size=192,
            mega_block_kernel_size=11,
        )


class TitaNet(nn.Module):
    """
    TitaNet is a neural network for extracting speaker representations,
    by leveraging 1D depth-wise separable convolutions with SE layers
    and a channel attention based statistic pooling layer

    "TitaNet: Neural Model for speaker representation with 1D Depth-wise
    separable convolutions and global context", Kologuri et al.,
    https://arxiv.org/abs/2110.04410
    """

    def __init__(
        self,
        n_mels,
        n_mega_blocks,
        n_sub_blocks,
        encoder_hidden_size,
        encoder_output_size,
        embedding_size,
        mega_block_kernel_size,
        prolog_kernel_size=3,
        epilog_kernel_size=1,
        se_reduction=16,
        dropout=0.5,
    ):
        super(TitaNet, self).__init__()

        # Define encoder and decoder
        self.encoder = Encoder(
            n_mels,
            n_mega_blocks,
            n_sub_blocks,
            encoder_hidden_size,
            encoder_output_size,
            mega_block_kernel_size,
            prolog_kernel_size=prolog_kernel_size,
            epilog_kernel_size=epilog_kernel_size,
            se_reduction=se_reduction,
            dropout=dropout,
        )
        self.decoder = Decoder(encoder_output_size, embedding_size)

    def forward(self, spectrograms):
        """
        Given input spectrograms of shape [B, M, T], TitaNet returns
        utterance-level embeddings of shape [B, E]

        B: batch size
        M: number of mel frequency bands
        T: maximum number of time steps (frames)
        E: embedding size
        """
        encodings = self.encoder(spectrograms)
        return self.decoder(encodings)


class Encoder(nn.Module):
    """
    The TitaNet encoder starts with a prologue block, followed by a number
    of mega blocks and ends with an epilogue block; all blocks comprise
    convolutions, batch normalization, activation and dropout, while mega
    blocks are also equipped with residual connections and SE modules

    "TitaNet: Neural Model for speaker representation with 1D Depth-wise
    separable convolutions and global context", Kologuri et al.,
    https://arxiv.org/abs/2110.04410
    """

    def __init__(
        self,
        n_mels,
        n_mega_blocks,
        n_sub_blocks,
        hidden_size,
        output_size,
        mega_block_kernel_size,
        prolog_kernel_size=3,
        epilog_kernel_size=1,
        se_reduction=16,
        dropout=0.5,
    ):
        super(Encoder, self).__init__()

        # Define encoder as sequence of prolog, mega blocks and epilog
        self.prolog = modules.ConvBlock1d(n_mels, hidden_size, prolog_kernel_size)
        self.mega_blocks = nn.Sequential(
            *[
                MegaBlock(
                    hidden_size,
                    hidden_size,
                    mega_block_kernel_size,
                    n_sub_blocks,
                    se_reduction=se_reduction,
                    dropout=dropout,
                )
                for _ in range(n_mega_blocks)
            ]
        )
        self.epilog = modules.ConvBlock1d(hidden_size, output_size, epilog_kernel_size)

    def forward(self, spectrograms):
        """
        Given input spectrograms of shape [B, M, T], return encodings
        of shape [B, DE, T]

        B: batch size
        M: number of mel frequency bands
        T: maximum number of time steps (frames)
        DE: encoding output size
        H: hidden size
        """
        # [B, M, T] -> [B, H, T]
        prolog_outputs = self.prolog(spectrograms)

        # [B, H, T] -> [B, H, T]
        mega_blocks_outputs = self.mega_blocks(prolog_outputs)

        # [B, H, T] -> [B, DE, T]
        return self.epilog(mega_blocks_outputs)


class MegaBlock(nn.Module):
    """
    The TitaNet mega block, part of its encoder, comprises a sequence
    of sub-blocks, where each one contains a time-channel separable
    convolution followed by batch normalization, activation and dropout;
    the output of the sequence of sub-blocks is then processed by a SE
    module and merged with the initial input through a skip connection


    "TitaNet: Neural Model for speaker representation with 1D Depth-wise
    separable convolutions and global context", Kologuri et al.,
    https://arxiv.org/abs/2110.04410
    """

    def __init__(
        self,
        input_size,
        output_size,
        kernel_size,
        n_sub_blocks,
        se_reduction=16,
        dropout=0.5,
    ):
        super(MegaBlock, self).__init__()

        # Store attributes
        self.dropout = dropout

        # Define sub-blocks composed of depthwise convolutions
        channels = [input_size] + [output_size] * n_sub_blocks
        self.sub_blocks = nn.Sequential(
            *[
                modules.ConvBlock1d(
                    in_channels,
                    out_channels,
                    kernel_size,
                    activation="relu",
                    dropout=dropout,
                    depthwise=True,
                )
                for in_channels, out_channels in zip(channels[:-1], channels[1:])
            ],
            modules.SqueezeExcitation(output_size, reduction=se_reduction)
        )

        # Define the final skip connection
        self.skip_connection = nn.Sequential(
            nn.Conv1d(input_size, output_size, kernel_size=1),
            nn.BatchNorm1d(output_size),
        )

    def forward(self, prolog_outputs):
        """
        Given prolog outputs of shape [B, H, T], return
        a feature tensor of shape [B, H, T]

        B: batch size
        H: hidden size
        T: maximum number of time steps (frames)
        """
        # [B, H, T] -> [B, H, T]
        mega_block_outputs = self.skip_connection(prolog_outputs) + self.sub_blocks(
            prolog_outputs
        )
        return F.dropout(
            F.relu(mega_block_outputs), p=self.dropout, training=self.training
        )


class Decoder(nn.Module):
    """
    The TitaNet decoder computes intermediate time-independent features
    using an attentive statistics pooling layer and downsamples such
    representation using two linear layers, to obtain a fixed-size
    embedding vector first and class logits afterwards


    "TitaNet: Neural Model for speaker representation with 1D Depth-wise
    separable convolutions and global context", Kologuri et al.,
    https://arxiv.org/abs/2110.04410
    """

    def __init__(self, encoder_output_size, embedding_size):
        super(Decoder, self).__init__()

        # Define the attention layer
        self.attention = nn.Sequential(
            AttentiveStatsPooling(encoder_output_size),
            nn.BatchNorm1d(encoder_output_size * 2),
        )

        # Define the final classifier
        self.linear = nn.Sequential(
            nn.Linear(encoder_output_size * 2, embedding_size),
            nn.BatchNorm1d(embedding_size),
        )

    def forward(self, encodings):
        """
        Given encoder outputs of shape [B, DE, T], return a tensor
        of shape [B, E]

        B: batch size
        T: maximum number of time steps (frames)
        DE: encoding output size
        E: embedding size
        """
        # [B, DE, T] -> [B, DE * 2]
        attention_context = self.attention(encodings)

        # [B, DE * 2] -> [B, E]
        return self.linear(attention_context)


class AttentiveStatsPooling(nn.Module):
    """
    The attentive statistics pooling layer uses an attention
    mechanism to give different weights to different frames and
    generates not only weighted means but also weighted variances,
    to form utterance-level features from frame-level features


    "Attentive Statistics Pooling for Deep Speaker Embedding",
    Okabe et al., https://arxiv.org/abs/1803.10963
    """

    def __init__(self, input_size, activation="relu"):
        super(AttentiveStatsPooling, self).__init__()

        # Store attributes
        self.input_size = input_size

        # Define architecture
        self.linear = nn.Linear(input_size, input_size)
        self.activation = nn.ReLU() if activation == "relu" else nn.Tanh()
        self.scores_w = nn.Parameter(torch.randn(input_size))
        self.scores_b = nn.Parameter(torch.zeros(1))

    def forward(self, encodings):
        """
        Given encoder outputs of shape [B, DE, T], return
        an attention context of shape [B, DE * 2]

        B: batch size
        T: maximum number of time steps (frames)
        DE: encoding output size
        """
        # Transpose input encodings
        # [B, DE, T] -> [B, T, DE]
        encodings = encodings.transpose(1, 2)
        batch_size, _, _ = encodings.shape

        # Compute attention scores of shape [B, T]
        projection = self.activation(self.linear(encodings))
        scores_w = (
            self.scores_w.unsqueeze(0).expand(batch_size, self.input_size).unsqueeze(2)
        )
        scores = projection.bmm(scores_w).squeeze() + self.scores_b

        # Compute attention weights of shape [B, T]
        weights = F.softmax(scores, dim=1)

        # Compute attention context of shape [B, T, DE]
        context = encodings * weights.unsqueeze(2).expand(-1, -1, self.input_size)

        # Compute pooling statistics (mean and variance)
        # each one of shape [B, DE]
        mean = torch.mean(context, dim=1)
        std = torch.sum(encodings * context, dim=1) - mean * mean

        # Return the concatenation of pooling statistics
        # of shape [B, DE * 2]
        return torch.cat((mean, std), dim=1)
