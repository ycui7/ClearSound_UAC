%% Minimum Phase Slow Rolloff filter
I_Factor = 8;
FS = 48e3;
F_cuttoff = 14.0e3;
F_reject = 40.0e3;
dB_Ripple_PassBand = 0.01;
dB_Rejection_StopBand = 80;
%% Generate interpolation filter using fdesign
d=fdesign.interpolator(I_Factor,'lowpass',F_cuttoff/FS/(I_Factor/2),F_reject/FS/(I_Factor/2), dB_Ripple_PassBand,dB_Rejection_StopBand);
opts = designopts(d,'ifir');
opts.UpsamplingFactor = I_Factor/2;
opts.JointOptimization = true;
Hd = design(d,'ifir', opts, 'Systemobject',true);
fvtool(Hd, 'NormalizedFrequency', 'off', 'Fs', FS*8);
%% Use frequency response of stage 1 to define minimum phase filter
[H,W] = freqz(Hd.Stage1.Numerator,1,FS);
A = abs(H);
F = W/(pi);
F_pass = F(1:10:ceil(F_cuttoff));
A_pass = A(1:10:ceil(F_cuttoff));
H_pass = H(1:10:ceil(F_cuttoff));
AN_pass = angle(H_pass);
R_pass = dB_Ripple_PassBand;
F_stop = [F_reject/FS 1];
A_stop = [10^(dB_Rejection_StopBand/-20) 10^(dB_Rejection_StopBand/-20)];
R_stop = 10^(dB_Rejection_StopBand/-20);
%% Create Stage1 with Minimum Phase FIR
e = fdesign.arbmag('B,F,A,R');
e.NBands = 2;
e.B1Frequencies = F_pass;
e.B1Amplitudes = A_pass;
e.B1Ripple = R_pass;
e.B2Frequencies = F_stop;
e.B2Amplitudes = A_stop;
e.B2Ripple = R_stop*4;
opts = designopts(e,'equiripple');
opts.MinPhase = 1;
opts.B1Weights = 1;
opts.B2Weights = 1e10;
He = design(e,'equiripple',opts);
fvtool(He, 'NormalizedFrequency', 'off', 'Fs', FS*2);
FIR1 = He.Numerator;
FIR2 = Hd.Stage2.Numerator;
%% Normalize Coefficients
FIR1 = (1/max(FIR1)).*FIR1;
FIR2 = (1/max(FIR2)).*FIR2;
%% Get DC Gain accounting for Interp Factor
FIR1_DC_Gain_dB = log10(sum(FIR1)/2)*20; %Interp 2x
FIR2_DC_Gain_dB = log10(sum(FIR2)/4)*20; %Interp 4x
FIR_DC_Gain_dB = FIR1_DC_Gain_dB + FIR2_DC_Gain_dB;
%% Scale DC Gain
FIR2_ATTEN = -0.0;%Trim this amount out of FIR2, and put into FIR1
MAX_MOD_INDEX = -0.936;
G1_dB = -FIR_DC_Gain_dB+MAX_MOD_INDEX-FIR2_ATTEN;
G2_dB = FIR2_ATTEN;
%% Scale Coeffs for Gain Requirements
G1 = (10^(G1_dB/20));
G2 = (10^(G2_dB/20));
stage1 = G1.*FIR1;
stage2 = G2.*FIR2;
% Verify Coefficients
if (length(stage1) > 512)
error('The stage1 filter must be 512 coefficients or less.');
end
if (length(stage2) > 28)
error('The stage2 filter must be 28 coefficients or less.');
end
coeff1 = [stage1(1:1:length(stage1)) zeros(1, 128-length(stage1))];
% Stage2 also exploits symmetry (either sine or cosine).
% Note: The coefficients must be zero padded to ensure the peak
% coefficient is at coeff2(14)
% Note: RAM is actually 16 coefficients, so pad by two zeros
L2 = round((length(stage2) + 1) / 2 - 0.5);
coeff2 = [zeros(1, 14 - L2) stage2(1:L2) zeros(1, 2)];
% Quantize the coefficients to 24-bit values
maxc = max(max(coeff1), max(coeff2));
coeff1 = coeff1 ./ (maxc / (2^23 - 1));
coeff2 = coeff2 ./ (maxc / (2^23 - 1));
% Check to ensure the DC gain correction did not overflow the second stage
if (max(abs(coeff2)) > 2^23-1)
error('The DC correction of the second stage forced a coefficient greater than 2^23-1');
end
% Write out the stage 1 coefficients
fid = fopen('stage1.txt', 'w');
for i = 1:length(coeff1)
fprintf(fid, '%.0f\n', coeff1(i));
end
fclose(fid);
% Write out the stage 2 coefficients
fid = fopen('stage2.txt', 'w');
for i = 1:length(coeff2)
fprintf(fid, '%.0f\n', coeff2(i));
end
fclose(fid);